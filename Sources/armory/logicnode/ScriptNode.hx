package armory.logicnode;

class ScriptNode extends LogicNode {

	public var property0:String;
	var result:Dynamic;

	public function new(tree:LogicTree) {
		super(tree);
	}

	override function run() {

		#if arm_hscript
		var expr = property0;
		var parser = new hscript.Parser();
		var ast = parser.parseString(expr);
		var interp = new hscript.Interp();
		result = interp.execute(ast);
		#end

		runOutputs(0);
	}

	override function get(from:Int):Dynamic {
		return result;
	}
}
