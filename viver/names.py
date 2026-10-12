"""Readable names of LabVIEW block diagram classes and wire color categories."""

# Heap class name -> name the user sees in LabVIEW
NODE_CLASS_NAMES = {
    "whileLoop": "While Loop",
    "forLoop": "For Loop",
    "timeLoop": "Timed Loop",
    "select": "Case Structure",
    "sequence": "Stacked Sequence",
    "flatSequence": "Flat Sequence",
    "sequenceFrame": "Sequence Frame",
    "timeFlatSequence": "Timed Sequence",
    "eventStruct": "Event Structure",
    "decomposeRecomposeStructure": "In Place Element",
    "commentNode": "Diagram Disable",
    "simNode": "Simulation",
    "mathScriptNode": "MathScript",
    "formulaNode": "Formula Node",
    "fBox": "Formula Node",
    "iUse": "SubVI",
    "polyIUse": "Polymorphic SubVI",
    "dynIUse": "Dynamic SubVI",
    "callParentDynIUse": "Call Parent Method",
    "callByRefNode": "Call By Reference",
    "propNode": "Property Node",
    "invokeNode": "Invoke Node",
    "extFunc": "Call Library Function",
    "codeVI": "Code Interface Node",
    "aBuild": "Build Array",
    "cABuild": "Build Array",
    "aIndx": "Index Array",
    "aReplace": "Replace Array Subset",
    "aInsert": "Insert Into Array",
    "aDelete": "Delete From Array",
    "aInit": "Initialize Array",
    "aReshape": "Reshape Array",
    "subset": "Array Subset",
    "concat": "Concatenate",
    "decimate": "Decimate Array",
    "interLeave": "Interleave Arrays",
    "mux": "Bundle",
    "nMux": "Bundle By Name",
    "demux": "Unbundle",
    "cpdArith": "Compound Arithmetic",
    "printf": "Format Into String",
    "scanf": "Scan From String",
    "uCast": "Type Cast",
    "mergeErrors": "Merge Errors",
    "eventRegNode": "Register For Events",
    "constructorNode": "Constructor",
    "sharedVariable": "Shared Variable",
    "leftFeedback": "Feedback Node",
    "rightFeedback": "Feedback Node",
    "initFeedback": "Feedback Init",
    "hiddenFBNode": "Feedback Node",
    "growArrayNode": "Array Node",
    "sharedGrowArrayNode": "Array Node",
    "decomposeArrayNode": "Array Split",
    "decomposeClusterNode": "Unbundle/Bundle Elements",
    "fxpUnbundle": "Fixed Point Unbundle",
    "exprNode": "Expression Node",
    "textNode": "Text Node",
    "xNode": "XNode",
    "externalNode": "External Node",
    "gRef": "Variable",
    "prim": "Primitive",
    "fPTerm": "Terminal",
    "const": "Constant",
}

# These classes are structures that contain inner diagrams (frames)
STRUCTURE_CLASSES = {
    "whileLoop", "forLoop", "timeLoop", "select", "sequence", "flatSequence",
    "timeFlatSequence", "eventStruct", "decomposeRecomposeStructure",
    "commentNode", "simNode", "externalStructNode", "xStructure", "sequenceFrame",
}

SUBVI_CLASSES = {"iUse", "polyIUse", "dynIUse", "callParentDynIUse", "genIUse"}

# Decoration classes that are not drawn
DECOR_CLASSES = {"attachment", "cosm", "multiCosm", "bigMultiCosm", "annex", "grid", "growViewObj"}

# Readable name for the control / indicator type (FP ddo class)
DDO_CLASS_NAMES = {
    "stdNum": "Numeric",
    "stdBool": "Boolean",
    "stdString": "String",
    "stdPath": "Path",
    "indArr": "Array",
    "stdClust": "Cluster",
    "stdRing": "Ring",
    "stdColorNum": "Color",
    "stdRefNum": "Refnum",
    "stdLvVariant": "Variant",
    "stdTable": "Table",
    "stdGraph": "Graph",
    "stdSlide": "Slide",
    "stdKnob": "Knob",
    "stdPict": "Picture",
    "stdListbox": "Listbox",
    "listbox": "Listbox",
    "tabControl": "Tab Control",
    "stdTag": "Tag",
    "typeDef": "TypeDef",
    "stdMeasureData": "Waveform",
    "stdComboBox": "Combo Box",
    "udClassDDO": "LV Class",
    "absTime": "Timestamp",
    "radioClust": "Radio Buttons",
    "treeControl": "Tree",
    "stdCont": "Container",
}


def wire_category(type_name):
    """Returns the LabVIEW wire color category for a data type name."""
    t = (type_name or "").lower()
    if not t or t == "void":
        return "void"
    if t == "boolean" or t.startswith("bool"):
        return "bool"
    if t.startswith("numfloat") or t.startswith("numcomplex") or "fixedpoint" in t:
        return "float"
    if t.startswith("numint") or t.startswith("numuint") or t.startswith("unitint") \
            or t.startswith("numenum") or t.startswith("unitenum"):
        return "int"
    if t.startswith("unitfloat") or t.startswith("unitcomplex"):
        return "float"
    if t == "string" or t == "cstring" or t == "pasc string":
        return "string"
    if t == "path":
        return "path"
    if t == "cluster":
        return "cluster"
    if t == "refnum":
        return "refnum"
    if "variant" in t:
        return "variant"
    if t == "array":
        return "array"
    if "measuredata" in t or "waveform" in t:
        return "cluster"
    return "other"
