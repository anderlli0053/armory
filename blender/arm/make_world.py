import os

import bpy

import arm.assets as assets
import arm.log as log
from arm.material import make_shader
from arm.material.parser_state import ParserState, ParserContext
from arm.material.shader import ShaderContext, Shader
import arm.material.cycles as cycles
import arm.node_utils as node_utils
import arm.utils
import arm.write_probes as write_probes

callback = None
shader_datas = []


def build():
    global shader_datas

    bpy.data.worlds['Arm'].world_defs = ''
    worlds = []
    shader_datas = []

    for scene in bpy.data.scenes:
        # Only export worlds from enabled scenes
        if scene.arm_export and scene.world is not None and scene.world not in worlds:
            worlds.append(scene.world)
            create_world_shaders(scene.world)


def create_world_shaders(world: bpy.types.World):
    """Creates fragment and vertex shaders for the given world."""
    global shader_datas
    world_name = arm.utils.safestr(world.name)
    pass_name = 'World_' + world_name

    shader_props = {
        'name': world_name,
        'depth_write': False,
        'compare_mode': 'less',
        'cull_mode': 'clockwise',
        'color_attachments': ['_HDR'],
        'vertex_elements': [{'name': 'pos', 'data': 'float3'}, {'name': 'nor', 'data': 'float3'}]
    }
    shader_data = {'name': world_name + '_data', 'contexts': [shader_props]}

    # ShaderContext expects a material, but using a world also works
    shader_context = ShaderContext(world, shader_data, shader_props)
    vert = shader_context.make_vert(custom_name="World_" + world_name)
    frag = shader_context.make_frag(custom_name="World_" + world_name)

    # Update name, make_vert() and make_frag() above need another name
    # to work
    shader_context.data['name'] = pass_name

    vert.add_out('vec3 normal')
    vert.add_uniform('mat4 SMVP', link="_skydomeMatrix")

    frag.add_include('compiled.inc')
    frag.add_in('vec3 normal')
    frag.add_out('vec4 fragColor')

    frag.write_attrib('vec3 n = normalize(normal);')

    vert.write('''normal = nor;
    vec4 position = SMVP * vec4(pos, 1.0);
    gl_Position = vec4(position);''')

    build_node_tree(world, frag, vert, shader_context)

    # TODO: Rework shader export so that it doesn't depend on materials
    # to prevent workaround code like this
    rel_path = os.path.join(arm.utils.build_dir(), 'compiled', 'Shaders')
    full_path = os.path.join(arm.utils.get_fp(), rel_path)
    if not os.path.exists(full_path):
        os.makedirs(full_path)

    # Output: World_[world_name].[frag/vert].glsl
    make_shader.write_shader(rel_path, shader_context.vert, 'vert', world_name, 'World')
    make_shader.write_shader(rel_path, shader_context.frag, 'frag', world_name, 'World')

    # Write shader data file
    shader_data_file = pass_name + '_data.arm'
    arm.utils.write_arm(os.path.join(full_path, shader_data_file), {'contexts': [shader_context.data]})
    shader_data_path = os.path.join(arm.utils.get_fp_build(), 'compiled', 'Shaders', shader_data_file)
    assets.add_shader_data(shader_data_path)

    assets.add_shader_pass(pass_name)
    assets.shader_passes_assets[pass_name] = shader_context.data
    shader_datas.append({'contexts': [shader_context.data], 'name': pass_name})


def build_node_tree(world: bpy.types.World, frag: Shader, vert: Shader, con: ShaderContext):
    """Generates the shader code for the given world."""
    world_name = arm.utils.safestr(world.name)
    world.world_defs = ''
    rpdat = arm.utils.get_rp()
    wrd = bpy.data.worlds['Arm']

    if callback is not None:
        callback()

    # film_transparent, do not render
    if bpy.context.scene is not None and bpy.context.scene.render.film_transparent:
        world.world_defs += '_EnvCol'
        frag.add_uniform('vec3 backgroundCol', link='_backgroundCol')
        frag.write('fragColor.rgb = backgroundCol;')
        return

    parser_state = ParserState(ParserContext.WORLD, world)
    parser_state.con = con
    parser_state.curshader = frag
    parser_state.frag = frag
    parser_state.vert = vert
    cycles.state = parser_state

    # Traverse world node tree
    is_parsed = False
    if world.node_tree is not None:
        output_node = node_utils.get_node_by_type(world.node_tree, 'OUTPUT_WORLD')
        if output_node is not None:
            is_parsed = parse_world_output(world, output_node, frag)

    # No world nodes/no output node, use background color
    if not is_parsed:
        solid_mat = rpdat.arm_material_model == 'Solid'
        if rpdat.arm_irradiance and not solid_mat:
            world.world_defs += '_Irr'
        col = world.color
        world.arm_envtex_color = [col[0], col[1], col[2], 1.0]
        world.arm_envtex_strength = 1.0

    # Irradiance/Radiance: clear to color if no texture or sky is provided
    if rpdat.arm_irradiance or rpdat.arm_irradiance:
        if '_EnvSky' not in world.world_defs and '_EnvTex' not in world.world_defs and '_EnvImg' not in world.world_defs:
            # Irradiance json file name
            world.arm_envtex_name = world_name
            world.arm_envtex_irr_name = world_name
            write_probes.write_color_irradiance(world_name, world.arm_envtex_color)

    # Clouds enabled
    if rpdat.arm_clouds and world.arm_use_clouds:
        world.world_defs += '_EnvClouds'
        # Also set this flag globally so that the required textures are
        # included
        wrd.world_defs += '_EnvClouds'
        frag_write_clouds(world, frag)

    if '_EnvSky' in world.world_defs or '_EnvTex' in world.world_defs or '_EnvImg' in world.world_defs or '_EnvClouds' in world.world_defs:
        frag.add_uniform('float envmapStrength', link='_envmapStrength')

    # Clear background color
    if '_EnvCol' in world.world_defs:
        frag.write('fragColor.rgb = backgroundCol;')

    elif '_EnvTex' in world.world_defs and '_EnvLDR' in world.world_defs:
        frag.write('fragColor.rgb = pow(fragColor.rgb, vec3(2.2));')

    if '_EnvClouds' in world.world_defs:
        frag.write('if (n.z > 0.0) fragColor.rgb = mix(fragColor.rgb, traceClouds(fragColor.rgb, n), clamp(n.z * 5.0, 0, 1));')

    if '_EnvLDR' in world.world_defs:
        frag.write('fragColor.rgb = pow(fragColor.rgb, vec3(1.0 / 2.2));')

    # Mark as non-opaque
    frag.write('fragColor.a = 0.0;')

    # Hack to make procedural textures work
    frag_bpos = (frag.contains('bposition') and not frag.contains('vec3 bposition')) or vert.contains('bposition')
    if frag_bpos:
        frag.add_in('vec3 bposition')
        vert.add_out('vec3 bposition')
        # Use normals for now
        vert.write('bposition = nor;')

    frag_mpos = (frag.contains('mposition') and not frag.contains('vec3 mposition')) or vert.contains('mposition')
    if frag_mpos:
        frag.add_in('vec3 mposition')
        vert.add_out('vec3 mposition')
        # Use normals for now
        vert.write('mposition = nor;')

    if frag.contains('texCoord') and not frag.contains('vec2 texCoord'):
        frag.add_in('vec2 texCoord')
        vert.add_out('vec2 texCoord')
        # World has no UV map
        vert.write('texCoord = vec2(1.0, 1.0);')


def parse_world_output(world: bpy.types.World, node_output: bpy.types.Node, frag: Shader) -> bool:
    """Parse the world's output node. Return `False` when the node has
    no connected surface input."""
    surface_node = node_utils.find_node_by_link(world.node_tree, node_output, node_output.inputs[0])
    if surface_node is None:
        return False

    parse_surface(world, surface_node, frag)
    return True


def parse_surface(world: bpy.types.World, node_surface: bpy.types.Node, frag: Shader):
    wrd = bpy.data.worlds['Arm']
    rpdat = arm.utils.get_rp()
    solid_mat = rpdat.arm_material_model == 'Solid'

    if node_surface.type in ('BACKGROUND', 'EMISSION'):
        # Append irradiance define
        if rpdat.arm_irradiance and not solid_mat:
            wrd.world_defs += '_Irr'

        # Extract environment strength
        # Todo: follow/parse strength input
        world.arm_envtex_strength = node_surface.inputs[1].default_value

        # Color
        out = cycles.parse_vector_input(node_surface.inputs[0])
        frag.write(f'fragColor.rgb = {out};')

        if not node_surface.inputs[0].is_linked:
            solid_mat = rpdat.arm_material_model == 'Solid'
            if rpdat.arm_irradiance and not solid_mat:
                world.world_defs += '_Irr'
            world.arm_envtex_color = node_surface.inputs[0].default_value
            world.arm_envtex_strength = 1.0

    else:
        log.warn(f'World node type {node_surface.type} must not be connected to the world output node!')

    # Invalidate the parser state for subsequent executions
    cycles.state = None


def frag_write_clouds(world: bpy.types.World, frag: Shader):
    """References:
    GPU PRO 7 - Real-time Volumetric Cloudscapes
    https://www.guerrilla-games.com/read/the-real-time-volumetric-cloudscapes-of-horizon-zero-dawn
    https://github.com/sebh/TileableVolumeNoise
    """
    frag.add_uniform('sampler3D scloudsBase', link='$clouds_base.raw')
    frag.add_uniform('sampler3D scloudsDetail', link='$clouds_detail.raw')
    frag.add_uniform('sampler2D scloudsMap', link='$clouds_map.png')
    frag.add_uniform('float time', link='_time')

    frag.add_const('float', 'cloudsLower', str(round(world.arm_clouds_lower * 100) / 100))
    frag.add_const('float', 'cloudsUpper', str(round(world.arm_clouds_upper * 100) / 100))
    frag.add_const('vec2', 'cloudsWind', 'vec2(' + str(round(world.arm_clouds_wind[0] * 100) / 100) + ',' + str(round(world.arm_clouds_wind[1] * 100) / 100) + ')')
    frag.add_const('float', 'cloudsPrecipitation', str(round(world.arm_clouds_precipitation * 100) / 100))
    frag.add_const('float', 'cloudsSecondary', str(round(world.arm_clouds_secondary * 100) / 100))
    frag.add_const('float', 'cloudsSteps', str(round(world.arm_clouds_steps * 100) / 100))

    frag.add_function('''float remap(float old_val, float old_min, float old_max, float new_min, float new_max) {
\treturn new_min + (((old_val - old_min) / (old_max - old_min)) * (new_max - new_min));
}''')

    frag.add_function('''float getDensityHeightGradientForPoint(float height, float cloud_type) {
\tconst vec4 stratusGrad = vec4(0.02f, 0.05f, 0.09f, 0.11f);
\tconst vec4 stratocumulusGrad = vec4(0.02f, 0.2f, 0.48f, 0.625f);
\tconst vec4 cumulusGrad = vec4(0.01f, 0.0625f, 0.78f, 1.0f);
\tfloat stratus = 1.0f - clamp(cloud_type * 2.0f, 0, 1);
\tfloat stratocumulus = 1.0f - abs(cloud_type - 0.5f) * 2.0f;
\tfloat cumulus = clamp(cloud_type - 0.5f, 0, 1) * 2.0f;
\tvec4 cloudGradient = stratusGrad * stratus + stratocumulusGrad * stratocumulus + cumulusGrad * cumulus;
\treturn smoothstep(cloudGradient.x, cloudGradient.y, height) - smoothstep(cloudGradient.z, cloudGradient.w, height);
}''')

    frag.add_function('''float sampleCloudDensity(vec3 p) {
\tfloat cloud_base = textureLod(scloudsBase, p, 0).r * 40; // Base noise
\tvec3 weather_data = textureLod(scloudsMap, p.xy, 0).rgb; // Weather map
\tcloud_base *= getDensityHeightGradientForPoint(p.z, weather_data.b); // Cloud type
\tcloud_base = remap(cloud_base, weather_data.r, 1.0, 0.0, 1.0); // Coverage
\tcloud_base *= weather_data.r;
\tfloat cloud_detail = textureLod(scloudsDetail, p, 0).r * 2; // Detail noise
\tfloat cloud_detail_mod = mix(cloud_detail, 1.0 - cloud_detail, clamp(p.z * 10.0, 0, 1));
\tcloud_base = remap(cloud_base, cloud_detail_mod * 0.2, 1.0, 0.0, 1.0);
\treturn cloud_base;
}''')

    func_cloud_radiance = 'float cloudRadiance(vec3 p, vec3 dir) {\n'
    if '_EnvSky' in world.world_defs:
        func_cloud_radiance += '\tvec3 sun_dir = hosekSunDirection;\n'
    else:
        func_cloud_radiance += '\tvec3 sun_dir = vec3(0, 0, -1);\n'
    func_cloud_radiance += '''\tconst int steps = 8;
\tfloat step_size = 0.5 / float(steps);
\tfloat d = 0.0;
\tp += sun_dir * step_size;
\tfor(int i = 0; i < steps; ++i) {
\t\td += sampleCloudDensity(p + sun_dir * float(i) * step_size);
\t}
\treturn 1.0 - d;
}'''
    frag.add_function(func_cloud_radiance)

    frag.add_function('''vec3 traceClouds(vec3 sky, vec3 dir) {
\tconst float step_size = 0.5 / float(cloudsSteps);
\tfloat T = 1.0;
\tfloat C = 0.0;
\tvec2 uv = dir.xy / dir.z * 0.4 * cloudsLower + cloudsWind * time * 0.02;

\tfor (int i = 0; i < cloudsSteps; ++i) {
\t\tfloat h = float(i) / float(cloudsSteps);
\t\tvec3 p = vec3(uv * 0.04, h);
\t\tfloat d = sampleCloudDensity(p);

\t\tif (d > 0) {
\t\t\t// float radiance = cloudRadiance(p, dir);
\t\t\tC += T * exp(h) * d * step_size * 0.6 * cloudsPrecipitation;
\t\t\tT *= exp(-d * step_size);
\t\t\tif (T < 0.01) break;
\t\t}
\t\tuv += (dir.xy / dir.z) * step_size * cloudsUpper;
\t}

\treturn vec3(C) + sky * T;
}''')
