from .circom import *
import numpy as np
from ..symbol import *
from ..transform import WithParameters
from .. import op, utils
from .model import visit as zkvisit

def register_op_map(op_name):
    def wrapper_func(f):
        def _wrapper(sym: Symbol):
            if sym.op_name != op_name:
                return {}
            return {sym.op_name: f(sym)}
        return _wrapper
    return wrapper_func

def map_binary_op(sym: Symbol, name) -> str:
    A_shape = list(sym.args[0].shape)
    B_shape = list(sym.args[1].shape)
    if A_shape == B_shape:
        return "Element{}D{}".format(len(A_shape), name)

    assert len(A_shape) == len(B_shape)
    max_dim = max(len(A_shape), len(B_shape))
    #  A_shape = [1]*max_dim + A_shape
    #  B_shape = [1]*max_dim + B_shape
    equal_dims = []
    for i, (sa, sb) in enumerate(zip(A_shape[-max_dim:], B_shape[-max_dim:])):
        if sa == sb and sa != 1:
            equal_dims.append(i)
        else:
            assert any([sa == 1, sb == 1])
    assert len(equal_dims) == 1, "{}: {} vs. {}".format(
            equal_dims, A_shape, B_shape)
    return "Broadcast{}DAxis{}{}".format(
            max_dim, equal_dims[0], name)

@register_op_map("subtract")
def map_subtract(sym: Symbol):
    return map_binary_op(sym, "Sub")

@register_op_map("add")
def map_add(sym: Symbol):
    return map_binary_op(sym, "Add")

def map_component(sym: Symbol) -> CircomGenerator:
    inputs = sym.args
    comp_map = {
        # "null": "Input",
        "var": "Input",

        "nn.relu": "ReLU{}D".format(len(sym.shape)),
        "nn.pad_scalar": "Pad2D",
        "nn.conv2d": "Conv2D_CHW",
        "nn.max_pool2d": "MaxPool2D",
        "nn.dense": "Dense2" if len(inputs) == 2 else "Dense",
        "nn.bias_add": "BiasAdd{}".format(len(sym.shape)),

        **map_subtract(sym),
        **map_add(sym),
        #  "add": "ElementAdd",

        "mul_scalar": "MulScalar",
        "add_scalar": "AddScalar",
        "subtract_scalar": "SubScalar",
        "clip": "Clip{}D".format(len(sym.shape)),

        "multiply": "MulScalar",
        "right_shift": "RightShift",
        "squeeze": "Squeeze_CHW",
        "sum": "Sum_CHW",
        "pass": "Pass{}D".format(len(sym.shape)),

        "image.resize2d": "Resize2D",

        "reshape": "ReShape{}D".format(len(sym.shape)),
        #  "reshape": "ReShape" + str(len(sym.attrs["shape"])) + "D",
        "flatten": "Flatten{}D".format(
            len(inputs[0].shape) if inputs else 0),
    }
    return components[comp_map[sym.op_name]]


def change_name(symbol, params):
    # change into valid circom symbol name
    new_params = {}
    def _change_name(sym: Symbol):
        if op.is_operator(sym, params):
            name = sym.name.replace("%", "O_")
            name = name.replace(".", "_")
        elif op.is_param(sym, params):
            name = sym.name.replace("%", "P_")
            name = name.replace(".", "_")
            new_params[name] = params[sym.name]
        else:
            name = sym.name.replace("%", "I_")
            name = name.replace(".", "_")

        sym = sym.copy(args=sym.args, name=name)
        return sym
    symbol = zkvisit(symbol, _change_name)
    params = new_params
    return symbol, params

def get_merged_attrs(symbol):
    attrs = {}
    #attrs = {k: v for k, v in symbol.attrs.items()}
    attrs.update(symbol.extra_attrs)
    attrs.update(symbol.attrs)
    return attrs

def model2circom(symbol, params) -> (CircomGenerator, typing.Dict[str, CircomGenerator]) :
    generator_map: typing.Dict[str, CircomGenerator] = {}
    circom_ops = set()
    def sym2circom(sym: Symbol):
        name = sym.name
        if name in generator_map:
            return

        inputs = [generator_map[i.name] for i in sym.args]

        attrs = get_merged_attrs(sym)
        #print("model2circom_transfering:: sym_name:{}, op_name:{}, attrs:{}".format(name, sym.op_name, attrs))

        # mrt ops fit with circom ops
        # insert pad2d before conv2d
        if (sym.op_name == "nn.conv2d" or sym.op_name=="nn.max_pool2d") and "padding" in attrs:
            padding = sym.attrs["padding"]
            sym_pad = sym.copy(args=sym.args)
            sym_pad.name = name+"_pad"
            sym_pad.op_name = "nn.pad_scalar"
            attrs_pad = get_merged_attrs(sym)
            attrs_pad["pad_value"] = 0
            shape_pad = inputs[0].shape
            shape_pad[1] = shape_pad[1] + padding[0] + padding[1]
            shape_pad[2] = shape_pad[2] + padding[2] + padding[3]
            attrs_pad["shape"] = shape_pad
            sym_pad.attrs = attrs_pad
            sym_pad.shape = shape_pad
            inputs_pad = [inputs[0]]
            sym_pad.args = inputs_pad

            # start generate map
            gen_pad = map_component(sym_pad)(sym_pad.name, inputs_pad, attrs_pad)
            circom_ops.add(gen_pad.comp.op_name)
            generator_map[sym_pad.name] = gen_pad

            sym.args[0] = sym_pad
            sym.attrs["shape"] = attrs["shape"]
            sym.shape = attrs["shape"]
            sym.attrs["padding"] = [0]
            # start generate map
            attrs = get_merged_attrs(sym)
            inputs = [generator_map[i.name] for i in sym.args]
            gen = map_component(sym)(name, inputs, attrs)
            circom_ops.add(gen.comp.op_name)
            generator_map[sym.name] = gen

        elif sym.op_name == "cast":
            sym_rs = sym.copy(args=sym.args)
            # change op_name
            sym_rs.op_name = "pass"
            sym2circom(sym_rs)

        # reshape auto infer newshape
        elif sym.op_name == "reshape":
            # reshape op
            sym_rs = sym.copy(args=sym.args)
            # when new shape -1, may occurs len(orig_shape) != 1, just flatten first, then reshape
            if len(sym.args[0].shape)>1:
                sym_fl = sym.copy(args=sym.args)
                sym_fl.name = name+"_flatten"
                sym_fl.op_name = "flatten"
                sym_fl.attrs["shape"] = [attrs["shape"][0]]
                sym_fl.shape = [attrs["shape"][0]]
                # start generate map
                attrs_fl = get_merged_attrs(sym_fl)
                inputs_fl = [generator_map[i.name] for i in sym_fl.args]
                gen = map_component(sym_fl)(sym_fl.name, inputs_fl, attrs_fl)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_fl.name] = gen
                # process reshape op
                sym_rs = sym.copy(args=[sym_fl])
                sym_rs.attrs["shape"] = attrs["shape"]
                sym_rs.shape = attrs["shape"]

            # start generate map
            attrs = get_merged_attrs(sym_rs)
            inputs = [generator_map[i.name] for i in sym_rs.args]
            gen = map_component(sym_rs)(sym_rs.name, inputs, attrs)
            circom_ops.add(gen.comp.op_name)
            generator_map[sym_rs.name] = gen

        elif sym.op_name == "mrt.rs_pclip":
            # rs op
            sym_rs = sym.copy(args=sym.args)
            sym_rs.op_name = "right_shift"
            sym_rs.name = name+"_right_shift"
            sym2circom(sym_rs)

            # pclip op
            sym_pclip = sym.copy(args=[sym_rs])
            sym_pclip.op_name = "mrt.pclip"
            sym_pclip.name = name
            sym2circom(sym_pclip)

        elif sym.op_name == "mrt.pclip":
            if len(sym.args[0].shape) == 1:
                # clip op
                sym.op_name = "clip"
                precision = sym.attrs["precision"]
                # todo calculate
                sym.attrs["a_max"] = abs(int(np.power(2, precision-1)-1))
                sym.attrs["a_min"] = -1 * sym.attrs["a_max"]
                sym.attrs["shape"] = sym.args[0].shape
                sym.shape = sym.args[0].shape
                # start generate map
                attrs = get_merged_attrs(sym)
                inputs = [generator_map[i.name] for i in sym.args]
                gen = map_component(sym)(sym.name, inputs, attrs)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym.name] = gen

            else:

                sym_flatten = sym.copy(args=sym.args)
                sym_flatten.name = name+"_flatten"
                sym_flatten.op_name = "flatten"
                # start generate map
                attrs_flatten = get_merged_attrs(sym_flatten)
                attrs_flatten["shape"] = [int(np.prod(sym.args[0].shape))]
                sym_flatten.shape = attrs_flatten["shape"]
                inputs = [generator_map[i.name] for i in sym_flatten.args]
                gen = map_component(sym_flatten)(sym_flatten.name, inputs, attrs_flatten)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_flatten.name] = gen

                # clip op
                sym_clip = sym_flatten.copy(args=[sym_flatten])
                sym_clip.op_name = "clip"
                sym_clip.name = name+"_clip"
                precision = sym.attrs["precision"]
                # MARK: all convert sym.shape = xxx
                sym_clip.attrs["shape"] = attrs_flatten["shape"]
                sym_clip.shape = attrs_flatten["shape"]
                sym_clip.attrs["a_max"] = abs(int(np.power(2, precision-1)-1))
                sym_clip.attrs["a_min"] = -1 * sym_clip.attrs["a_max"]
                attrs_clip = get_merged_attrs(sym_clip)
                # start generate map
                inputs = [generator_map[i.name] for i in sym_clip.args]
                gen = map_component(sym_clip)(sym_clip.name, inputs, attrs_clip)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_clip.name] = gen

                # reshape op
                sym_reshape = sym_clip.copy(args=[sym_clip])
                sym_reshape.name = name
                sym_reshape.op_name = "reshape"
                sym_reshape.attrs["shape"] = sym_flatten.args[0].shape
                sym_reshape.shape = sym_flatten.args[0].shape
                # start generate map
                attrs_reshape = get_merged_attrs(sym_reshape)
                inputs = [generator_map[i.name] for i in sym_reshape.args]
                gen = map_component(sym_reshape)(sym_reshape.name, inputs, attrs_reshape)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_reshape.name] = gen

        elif sym.op_name == "multiply" or sym.op_name == "right_shift":
            if len(sym.args[0].shape) == 1:
                sym_rs = sym.copy(args=[sym.args[0]])
                attrs["scalar"] = int(params[sym.args[1].name].numpy())
                inputs = [generator_map[i.name] for i in sym_rs.args]
                gen = map_component(sym_rs)(name, inputs, attrs)
                circom_ops.add(gen.comp.op_name)
                generator_map[name] = gen
            else:

                sym_flatten = sym.copy(args=[sym.args[0]])
                sym_flatten.name = name+"_flatten"
                sym_flatten.op_name = "flatten"
                sym_flatten.attrs["shape"] = [int(np.prod(sym.args[0].shape))]
                sym_flatten.shape = [int(np.prod(sym.args[0].shape))]
                # start generate map
                attrs_flatten = get_merged_attrs(sym_flatten)
                inputs = [generator_map[i.name] for i in sym_flatten.args]
                gen = map_component(sym_flatten)(sym_flatten.name, inputs, attrs_flatten)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_flatten.name] = gen

                # rs op
                sym_rs = sym_flatten.copy(args=[sym_flatten])
                sym_rs.op_name = sym.op_name
                sym_rs.name = name+"_right_shift"
                # start generate map
                attrs_rs = get_merged_attrs(sym_rs)
                attrs_rs["scalar"] = int(params[sym.args[1].name].numpy())
                inputs = [generator_map[i.name] for i in sym_rs.args]
                gen = map_component(sym_rs)(sym_rs.name, inputs, attrs_rs)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_rs.name] = gen

                # reshape op
                sym_reshape = sym_rs.copy(args=[sym_rs])
                sym_reshape.name = name
                sym_reshape.op_name = "reshape"
                sym_reshape.attrs["shape"] = sym_flatten.args[0].shape
                sym_reshape.shape = sym_flatten.args[0].shape
                # start generate map
                attrs_reshape = get_merged_attrs(sym_reshape)
                inputs = [generator_map[i.name] for i in sym_reshape.args]
                gen = map_component(sym_reshape)(sym_reshape.name, inputs, attrs_reshape)
                circom_ops.add(gen.comp.op_name)
                generator_map[sym_reshape.name] = gen

        else:
            gen = map_component(sym)(name, inputs, attrs)
            circom_ops.add(gen.comp.op_name)
            generator_map[name] = gen
        #  comp.fill_circom()

    visit(symbol, sym2circom)
    #  print("Invoked Circoms: \n", "\n".join(circom_ops))

    out = components["Output"](
            "out", [generator_map[symbol.name]],
            { "shape": generator_map[symbol.name].shape })
    return (out, generator_map)

def assert_rs(symbol: Symbol):
    @filter_operators("right_shift")
    def _assert(sym: Symbol):
        B: WithParameters = sym.args[1]
        sym.extra_attrs["shift_bit"] = B.numpy().asscalar()
        assert B.is_variable()

def _shape_adapter(sym: Symbol):
    supported_ops = [
        "clip",
        "mul_scalar", "add_scalar", "subtract_scalar",
        "right_shift",
    ]
    if sym.op_name not in supported_ops:
        args = []
        for inp in sym.args:
            if "orig_shape" in inp.extra_attrs:
                inp = op.reshape(inp, newshape=
                    inp.extra_attrs["orig_shape"],
                    )
                #  inp = Symbol("reshape", inp.name + "_r",
                #          [ inp, ], [],
                #          { "shape": inp.attrs["orig_shape"],
                #              "dtype": sym.attrs["dtype"],
                #          })
            args.append(inp)
        sym = sym.copy(args=args)
        return sym

    if len(sym.shape) == 1:
        return

    input_shape = list(sym.args[0].shape)
    orig_shape = list(sym.args[0].extra_attrs.get(
        "orig_shape", input_shape))
    shape_one = utils.product(input_shape)

    args = [a for a in sym.args]
    if len(args[0].shape) != 1:
        args[0] = op.reshape(
            args[0], newshape=( shape_one, ))
        #  inp = Symbol("flatten", sym.args[0].name + "_f",
        #          [ sym.args[0], ], [],
        #          { "shape": ( shape_one, ),
        #              "dtype": sym.attrs["dtype"], })

    #  assert list(sym.attrs["shape"]) == input_shape, sym.info()
    sym = sym.copy(args=args)
    sym.extra_attrs.update({
        #  "shape": ( shape_one, ),
        "orig_shape": orig_shape,
        })
    return sym

def shape_adapter(symbol: Symbol):
    with utils.N("shape_adapter"):
        symbol = transform(symbol, _shape_adapter)

        if "orig_shape" in symbol.extra_attrs:
            symbol = op.reshape(symbol,
                    newshape= symbol.extra_attrs["orig_shape"], )
            #  symbol = Symbol("reshape", symbol.name + "_r",
            #          [ symbol, ], [],
            #          {
            #              "shape": symbol.attrs["orig_shape"],
            #              "dtype": symbol.attrs["dtype"],
            #          })

        def _clean_attrs(sym: Symbol):
            if "orig_shape" in sym.extra_attrs:
                del sym.extra_attrs["orig_shape"]

        visit(symbol, _clean_attrs)
    return symbol