
import numpy as np
from dataclasses import dataclass

from . import op
from .opns import *
from .precision import *
from .utils import number_to_bits
from .attrs import PClipAttrs, RequantAttrs
from .symbol import filter_operators
from .transform import Transformer, Pass

@dataclass(repr=False)
class MapMRTOp(Pass, QuantizedInfo):
    def identity(self):
        return self

    def map_pclip(self):
        X: FixPoint = self.args[0]

        pos = self.int_max()
        out = op.clip(X, a_min=-pos, a_max=pos)
        return out

    def map_rs_pclip(self):
        X, B = self.args
        out = op.right_shift(X, B)
        pos = self.int_max()
        out = op.clip(out, a_min=-pos, a_max=pos)
        return out

MapMRTOp.test_all(MapMRTOp.identity)
MapMRTOp.test(op.RS_PCLIP)(MapMRTOp.map_rs_pclip)
MapMRTOp.test(op.PCLIP)(MapMRTOp.map_pclip)


@dataclass(repr=False)
class FixPoint(Transformer, QuantizedInfo):
    def __repr__(self, **attrs):
        attrs.setdefault("tinfer", self.dtype)
        attrs.setdefault("sinfer", self.shape)
        return super().__repr__(**attrs)

    @classmethod
    def update_dict(cls, data, **kwargs) -> dict:
        data.update(kwargs)
        # prec = InferPrecision.from_dict(data)()
        # data["precision"] = prec or data["precision"]
        return data

    def like(self, other: Symbol, copy=False, **kwargs):
        return super().like(other, copy=copy, **kwargs)

    def map_requant(self):
        X: FixPoint = self.args[0]
        parsed: RequantAttrs = self.parsed

        anno_bit = WithPrecision.MAX_BIT // 2
        if X.precision > anno_bit:
            rs_bit = X.from_const_data(X.precision - anno_bit)
            X = op.right_shift(X, rs_bit).like(self)
            X.precision = anno_bit

        frac, exp = cvm_float(self.parsed.rescale, anno_bit)
        assert frac >= 1
        assert exp <= 0
        frac_sym = X.from_const_data(frac)
        out = op.mul(X, frac_sym).like(self)

        exp_sym = out.from_const_data(-exp)
        # out = op.rs_pclip(out, exp_sym,
        #         precision=self.precision)
        pos = self.int_max()
        out = op.right_shift(out, exp_sym).like(self)
        out = op.clip(out, a_min=-pos, a_max=pos).like(self)
        return out

    def map_pclip(self):
        X: FixPoint = self.args[0]

        pos = self.int_max()
        out = op.clip(X, a_min=-pos, a_max=pos).like(self)
        return out

    def set_dtype(self):
        assert 0 <= self.precision and self.precision <= 32
        dtype = "int8" if self.precision <= 8 else "int32"
        self.attrs["dtype"] = dtype

    def match_dtype(self, out: Symbol):
        if self.precision <= 8:
            out = op.astype(out, target="int8")
        return out

    def __call__(self):
        self.validate_precision()

        self.set_dtype()
        out = self
        if self.is_input():
            pass
        elif self.is_param():
            data = np.round(self.numpy()).astype(self.dtype)
            absmax = np.abs(data).max()
            assert absmax <= self.int_max()
            out = self.from_np_data(data)
        elif self.is_op(PCLIP):
            out = self.map_pclip()
        elif self.is_op(REQUANT):
            out = self.map_requant()
        elif self.is_op(CONV2D, DENSE):
            out.attrs["out_dtype"] = "int32"

        if self.is_operator():
            out = self.match_dtype(out)

        inames = [a.name for a in self.args]
        tmp = op.subgraph(out, inames)
        tmp = op.infer_type(tmp)
        assert self.dtype == tmp.dtype, (
                "expected {}, but get {}, in \n{}"
        ).format(self.dtype, tmp.dtype, tmp)
        return out.like(self, copy=True)

def cvm_float(number, bits=24):
    """ Recalculate the float value within the given range of bits.

        Parameters
        __________
        number : float
            The input float value.
        bits : int
            The target bits to represent the value.

        Returns
        _______
        ret : tuple
            The recalculated float value with its corresponding bits to be shifted.
    """
    alpha = max((2 ** (bits - 1)) - 1, 1)
    bits -= 1
    assert number >= 0
    if number == 0:
        return 0, 0
    exp = 0
    while (number >= 1):
        number /= 2
        exp += 1
    while (number < 1):
        number *= 2
        exp -= 1
    while (bits > 1):
        if (int(number) == number):
            break
        number *= 2
        exp -= 1
        bits -= 1
    frac, sb = round(number), exp
    return min(frac, alpha), sb
