import tvm
from tvm import relay, ir
from tvm.relay import testing
from tvm.mrt.utils import *

from tvm.mrt import api, runtime, image, extool, data
from tvm.mrt import stats, dataset
from tvm.mrt import utils

import sys
import numpy as np

batch_size = 1

def load_model_from_mx() -> (ir.IRModule, ParametersT):
    import mxnet as mx
    spath, ppath = gluon.save_model("resnet18_v1", ctx=mx.cpu())
    print(spath, ppath)
    symbol, params = gluon.load_model(spath, ppath)
    return relay.frontend.from_mxnet(symbol, arg_params=params)



if False:
    num_class = 10
    image_shape = (1, 28, 28)
    mod, params = testing.mlp.get_workload(
            num_classes=num_class,
            image_shape=image_shape,
            batch_size=batch_size)
else:
    num_class = 1000
    image_shape = (3, 224, 224)
    out_shape = (batch_size, num_class)
    #  mod, params = load_model_from_mx()
    #  mod, params = testing.resnet.get_workload(
    #          batch_size=batch_size,
    #          num_classes=num_class,
    #          num_layers=18,
    #          image_shape=image_shape,)

data_shape = (batch_size,) + image_shape

def load_model_from_torch() -> (ir.IRModule, ParametersT):
    import torch
    from torchvision import models

    weights = models.ResNet18_Weights.IMAGENET1K_V1
    model = models.resnet18(weights=weights)
    model = model.eval()
    input_data = torch.randn(data_shape)
    script_module = torch.jit.trace(model, [input_data]).eval()
    return relay.frontend.from_pytorch(
            script_module, [ ("input", data_shape) ])

mod, params = load_model_from_torch()

mod: tvm.IRModule = mod
func: relay.function.Function = mod["main"]
expr: ir.RelayExpr = func.body

#  expr.simple_raw_print(mod["main"].body, params)


relay.Var
relay.var
relay.nn.conv2d
relay.nn.batch_flatten
relay.nn.batch_norm
relay.Tuple
relay.TupleGetItem
relay.expr.TupleWrapper
ir.tensor_type.TensorType
ir.type.TupleType

# mrt_model = model.from_mod(mod, params)
# mrt_model = mrt_model.set_input_shape((16,) + image_shape)
# mrt_model.print()
# mod = mrt_model.to_mod()
# mod: tvm.IRModule = relay.transform.InferType()(mod)
# print(mod.astext(show_meta_data=False))

#  tr = api.Trace("init", expr, params).infer_type()

from tvm.mrt import trace
from tvm.mrt.symbol import *
tr = trace.Trace.from_expr(expr, params)

@filter_operators(TUPLE_GET_ITEM_NAME)
def fuse_batch_norm(expr: relay.expr.Call, params: ParametersT):
    if extool.op_name(expr.tuple_value) == "nn.batch_norm":
        return expr.tuple_value.args[0]
    assert False

#  tr = tr.transform(fuse_batch_norm)

from tvm.mrt.calibrate import Calibrator
#  def calibrate(sym: Symbol, params: ParametersT):
#      #  print("apply calibrate for {}".format(sym))
#      data = None
#      if is_input(sym, params):
#          data = np.random.randn(*sym.shape).astype(sym.dtype)
#          data = tvm.nd.array(data)
#      elif is_param(sym, params):
#          data = params[sym.name]
#      return sym.clone(Calibrator, init_data=data)

tvm.nd.NDArray
tr.print()
#  calibrate_tr = tr.transform(calibrate)
calibrate_tr = tr.transform(Calibrator.apply())

print("\n\n\n")
def _cast(sym: Calibrator, params: ParametersT):
    print("cast: ", sym.output[0].shape)
calibrate_tr.transform(_cast)
sys.exit(1)
# ctx = tvm.runtime.cuda(1)

from tvm.mrt.fuse import FusionOp
def fuse(sym: Symbol, params: ParametersT):
    return sym.clone(FusionOp, params=params)

fuse_tr = tr.transform(fuse)

sys.exit(1)

#  print("\n", expr.astext(show_meta_data=False))
from torch.utils.data import DataLoader
import torch
from torch.utils.data import Dataset
from torchvision import datasets
from torchvision.transforms import ToTensor
import PIL

def to_tensor(img: PIL.Image.Image):
    img = img.resize(image_shape[1:])
    img = np.array(img).astype("float32")
    img = np.transpose(img, (2, 1, 0))
    return img

val_data = datasets.ImageFolder(
        path.join(utils.MRT_DATASET_ROOT, "imagenet/val"),
        transform=to_tensor)
data_loader = DataLoader(val_data, batch_size=1)

#  class TorchImageNet(dataset.Dataset):
#      def __init__(self):
#          self.data_loader = data_loader
#          self._max = len(self.data_loader)
#          self.reset()

#      def reset(self):
#          self._iter = iter(self.data_loader)

#      def next(self):
#          try:
#              data, label = next(self._iter)
#              return data.numpy(), label.numpy()
#          except Exception as e:
#              return None

#  data, label = next(iter(data_loader))
#  data, label = data.numpy(), label.numpy()
#  print(type(data), data.shape, type(label), label)
#  sys.exit(1)

# tr.print()
# outs = tr.calibrate()
# print(outs.keys())

# tr_eval = tr.eval(ctx)
# runtime.multiple_validate(tr_eval, TorchImageNet(),
#         stats.ClassificationOutput,)

# test accuracy
#  data = image.get_real_image(*image_shape[1:])
res = tr.run(data, device=ctx)
#  res = mrt_model.run(data)
#  print(res.shape, res.dtype)
# input_data = data.random_inputs(new_expr, params)
# res = runtime.infer(new_expr, input_data)
out = stats.ClassificationOutput()
out.merge([res[0], [0,]])
out.dl_info()
print("labels: ", dataset.ImageNet().labels(out.dl_top5[0]))

# fuse pass: fold_constant, fuse_batch_norm, quantize

# compare accuracy

# to_cvm

# for k, v in params.items():
#     print(k, type(v))
#     continue
# set show_meta_data=True if you want to show meta data
# print(mod.astext(show_meta_data=False))

# @ir.transform.module_pass(opt_level=2)
# def transform(mod, ctx):
#     tp = relay.TensorType((10,), "float32")
#     x = relay.var("x", tp)
#     func = relay.Function([x], relay.abs(x))
#     gv = relay.GlobalVar("myabs")
#     # new_mod = tvm.IRModule({gv: func})
#     new_mod = tvm.IRModule()
#     new_mod["myabs"] = func
#     new_mod.update(mod)
#     return new_mod

# print(relay.analysis.all_vars(mod["main"]))

# module_pass = transform
# assert isinstance(module_pass, ir.transform.ModulePass)
# assert module_pass.info.opt_level == 2

x = relay.var("x", shape=(1, 3, 28, 28), dtype="float32")
y = relay.var("y", shape=(28,), dtype="float32")
out = x + y
out = relay.abs(out)
a = relay.Constant(tvm.nd.array(np.ones((28,), dtype="float32")))
b = relay.Constant(tvm.nd.array(np.ones((28,), dtype="float32")))
c = a + b
out = out + c
relay.analysis.post_order_visit(out, _collect_ops)

mod = tvm.IRModule()
mod["main"] = relay.Function([x, y], out)
mod = relay.transform.FoldConstant()(mod)

print(mod.astext(show_meta_data=False))
sys.exit(1)

# mod = tvm.IRModule()
# mod["main"] = relay.Function([x, y], out)
# print(str(mod))

# mod = module_pass(mod)
# print("2", str(mod))

# # out = mod["myabs"](out)
# # mod["main"] = relay.Function([x, y], out)
# # print("1", str(mod))

# # mod = create_relay_module_from_model() # Output: Figure 1
import pprint
from tvm.relay.op.contrib import register
from tvm.relay.op.contrib import cvm
pattern_table = register.get_pattern_table("cvm")
pprint.pprint([p[0] for p in pattern_table])
mod = relay.transform.MergeComposite(pattern_table)(mod)
#  mod = relay.transform.AnnotateTarget(["dnnl"])(mod) # Output: Figure 2
#  mod = relay.transform.MergeCompilerRegions()(mod) # Output: Figure 3
#  mod = relay.transform.PartitionGraph()(mod) # Output: Figure 4
print("3", mod.astext(show_meta_data=False))
