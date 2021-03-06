from arm.logicnode.arm_nodes import *

class VectorClampToSizeNode(ArmLogicTreeNode):
    """Keeps the vector value inside the given range."""
    bl_idname = 'LNVectorClampToSizeNode'
    bl_label = 'Vector Clamp'
    arm_section = 'vector'
    arm_version = 1

    def init(self, context):
        super(VectorClampToSizeNode, self).init(context)
        self.add_input('NodeSocketVector', 'Vector In', default_value=[0.0, 0.0, 0.0])
        self.add_input('NodeSocketFloat', 'Min')
        self.add_input('NodeSocketFloat', 'Max')

        self.add_output('NodeSocketVector', 'Vector Out')
