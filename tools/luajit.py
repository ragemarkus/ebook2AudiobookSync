#!/usr/bin/env python3
"""Tiny LuaJIT runner (the box has no system luajit): luajit.py script.lua args..."""
import sys, lupa.luajit21 as L
rt = L.LuaRuntime(unpack_returned_tuples=True)
argt = rt.table(*sys.argv[2:]); argt[0] = sys.argv[1]
rt.globals().arg = argt
rt.execute(open(sys.argv[1]).read())
