"""Чтение измерений счётчиков Wiren Board WB-MAP3E / WB-MAP12E (прошивка 2.15).

Адреса регистров даны для канала 1. Для канала N: адрес + (N-1)*0x1000,
кроме напряжений — они общие для всех каналов счётчика.
Множители проверены 20.11.2026: сумма P по фазам совпадает с P total.
"""
from .modbus import s16, s32, u64

CH_STEP = 0x1000
REG_U = 0x1410        # U L1..L3: регистры 0x1410, 0x1412, 0x1414; ×0.01 В
REG_I = 0x1416        # I L1..L3: 0x1416, 0x1418, 0x141A; ×0.016 А
REG_P = 0x1300        # P total, L1, L2, L3: s32; ×5.12e-6 кВт
REG_PF = 0x10BC       # PF total, L1, L2, L3: s16; ×0.001
REG_E_IMP = 0x1A00    # энергия приём: u64; ×1e-5 кВт·ч
REG_E_EXP = 0x1A10    # энергия отдача: u64; ×1e-5 кВт·ч


def read_voltages(link, unit):
    w = link.read_input(unit, REG_U, 6)
    return [w[0] * 0.01, w[2] * 0.01, w[4] * 0.01]


def read_channel(link, unit, channel):
    off = (channel - 1) * CH_STEP
    wi = link.read_input(unit, REG_I + off, 6)
    wp = link.read_input(unit, REG_P + off, 8)
    wpf = link.read_input(unit, REG_PF + off, 4)
    wei = link.read_input(unit, REG_E_IMP + off, 4)
    wee = link.read_input(unit, REG_E_EXP + off, 4)
    p = [s32(wp[k], wp[k + 1]) * 5.12e-06 for k in (0, 2, 4, 6)]
    pf = [s16(x) * 0.001 for x in wpf]
    return {
        "i1": wi[0] * 0.016, "i2": wi[2] * 0.016, "i3": wi[4] * 0.016,
        "p_total": p[0], "p1": p[1], "p2": p[2], "p3": p[3],
        "pf": pf[0],
        "e_import": u64(wei) * 1e-05, "e_export": u64(wee) * 1e-05,
    }
