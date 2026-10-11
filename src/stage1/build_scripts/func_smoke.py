import glob, json, os, subprocess, tempfile, threading

F0 = "/opt/f0"
RAS = F0 + "/run_agent.sh"
base = os.environ.get("D2A_MODEL_BASE_URL", "")
mname = os.environ.get("D2A_MODEL_NAME", "")
key = os.environ.get("D2A_MODEL_API_KEY", "")
_NB = json.dumps(
    {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# MD-KEEP-TOP\n"]},
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": ["print('FIRST-KEEP')\n"],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": ["print('OLD-MIDDLE')\n"],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
                "source": ["print('THIRD-KEEP')\n"],
            },
            {"cell_type": "markdown", "metadata": {}, "source": ["## MD-KEEP-BOTTOM\n"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
)
_BIG = "".join(
    ("MARKER-L1500 please-change-me\n" if i == 1500 else "line %04d filler text\n" % i for i in range(2000))
)
_BIGCRLF = "".join(("crlf line %04d\r\n" % i for i in range(600)))
_GREP_NEEDLE_FILES = {"f037.txt", "f137.txt", "f199.txt"}
_GREP_SEED = {
    "f%03d.txt"
    % i: "NEEDLE-7731-XYZ\n" if "f%03d.txt" % i in _GREP_NEEDLE_FILES else "filler line %03d\n" % i
    for i in range(200)
}
_HUGE = "X" * 2000000 + "ENDMARK-7731"
TESTS = [
    {
        "id": "file_ops",
        "seed": {
            "config.txt": "name = alpha\nvalue = OLDVAL\nflag = true\n",
            "data.txt": "FIRSTLINE-AAA\n",
            "obsolete.txt": "junk\n",
            "keep.txt": "KEEPME-3344\n",
            "movable.txt": "MOVE-ME-5150\n",
        },
        "turns": [
            {
                "task": "Do ALL of the following in this repository, then stop:\n1) Create a file at path logs/app.log (create the logs directory if needed) with content exactly: STARTED-7788\n2) In config.txt, change OLDVAL to NEWVAL (keep the rest of the file).\n3) Append a new line containing SECONDLINE-BBB to data.txt (keep its existing content).\n4) Delete the file obsolete.txt. Do NOT touch keep.txt.\n5) MOVE movable.txt into a new subdirectory archive/ AND rename it, so it ends up at archive/moved.txt with its original content preserved; movable.txt must no longer exist at the top level."
            }
        ],
        "checks": [
            {"file": "logs/app.log", "contains": ["STARTED-7788"]},
            {"file": "config.txt", "contains": ["NEWVAL"], "absent": ["OLDVAL"]},
            {"file": "data.txt", "contains": ["FIRSTLINE-AAA", "SECONDLINE-BBB"]},
            {"deleted": "obsolete.txt"},
            {"file": "keep.txt", "contains": ["KEEPME-3344"]},
            {"file": "archive/moved.txt", "contains": ["MOVE-ME-5150"]},
            {"deleted": "movable.txt"},
        ],
    },
    {
        "id": "search_replace",
        "seed": {
            "a.py": "x = ALPHA\n",
            "b.py": "y = ALPHA\n",
            "c.txt": "z: ALPHA\n",
            "notes.txt": "TODO fix\nok\nTODO test\nTODO ship\ndone\n",
        },
        "turns": [
            {
                "task": "Do BOTH, then stop:\n1) Replace every occurrence of ALPHA with OMEGA in all files in the repository.\n2) Count how many lines in notes.txt contain the word TODO, and write just that number (digits only) into count.txt."
            }
        ],
        "checks": [
            {"file": "a.py", "contains": ["OMEGA"], "absent": ["ALPHA"]},
            {"file": "b.py", "contains": ["OMEGA"], "absent": ["ALPHA"]},
            {"file": "c.txt", "contains": ["OMEGA"], "absent": ["ALPHA"]},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "c=open('count.txt',encoding='utf-8').read().strip(); assert c=='3', ('count.txt must be exactly 3', repr(c)); print('VOK')",
        ],
    },
    {
        "id": "bugfix",
        "seed": {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "from calc import add\n\nif __name__ == '__main__':\n    assert add(2, 3) == 5\n    print('OK')\n",
        },
        "turns": [
            {
                "task": "There is a bug in calc.py: add() returns the wrong result. Fix calc.py so that add() returns the sum of its two arguments, then run the test to confirm. Do not modify the test file."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys; sys.path.insert(0, '.'); import calc; assert calc.add(2,3)==5 and calc.add(10,20)==30; print('VOK')",
        ],
    },
    {
        "id": "iter_fix_until_all_green",
        "seed": {
            "calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n",
            "test_calc.py": "from calc import add, mul\n\nif __name__ == '__main__':\n    assert add(2, 3) == 5\n    assert mul(2, 3) == 6\n    print('OK')\n",
        },
        "turns": [
            {
                "budget": {"max_steps": 18},
                "task": "Run the test; it currently fails. Fix calc.py so that ALL assertions pass. Re-run the test after every change to confirm. Do NOT modify test_calc.py. When all tests pass, create FIXED.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "FIXED.txt", "contains": ["RAN-OK"]},
            {"file": "test_calc.py", "contains": ["assert add(2, 3) == 5", "assert mul(2, 3) == 6"]},
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys; sys.path.insert(0,'.'); import calc; assert calc.add(2,3)==5 and calc.add(-1,4)==3 and calc.mul(2,3)==6 and calc.mul(4,5)==20; print('VOK')",
        ],
    },
    {
        "id": "bugfix_forbidden_file_and_no_commit",
        "seed": {
            "AGENTS.md": "# Project rules\n- NEVER modify rates.py (it is auto-generated).\n- NEVER run `git commit`.\n",
            "rates.py": "MULTIPLIER = 2",
            "billing.py": "from rates import MULTIPLIER\n\n\ndef charge(x):\n    return x + MULTIPLIER\n",
        },
        "turns": [
            {
                "task": "charge() computes the wrong total. Fix it so that charge(5)==10 and charge(3)==6. Strictly obey AGENTS.md. When done, create NOTE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "NOTE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "bash",
            "-c",
            'python -c "import sys;sys.path.insert(0,\'.\');import billing;assert billing.charge(5)==10 and billing.charge(3)==6" && test "$(cat rates.py)" = "MULTIPLIER = 2" && test "$(git rev-list --count HEAD)" = "1"',
        ],
    },
    {
        "id": "rename_preserve_string_literal_and_runs",
        "seed": {
            "pkg/__init__.py": "",
            "pkg/core.py": 'def foo():\n    return 41\n\n\nLABEL = "foo"\n',
            "pkg/use.py": "from pkg.core import foo\n\n\ndef run():\n    return foo() + 1\n",
            "main.py": "import pkg.use as u\n\nprint(u.run())\n",
        },
        "turns": [
            {
                "task": 'Rename the FUNCTION foo to fetch everywhere: its definition in pkg/core.py and all code references (pkg/use.py). The string literal "foo" assigned to LABEL in core.py MUST stay exactly "foo" (do not touch it). When done, create RENAMED.txt with content exactly: RAN-OK'
            }
        ],
        "checks": [{"file": "RENAMED.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "bash",
            "-c",
            "python -c \"import pkg.core as c;assert hasattr(c,'fetch') and not hasattr(c,'foo');assert c.LABEL=='foo'\" && python -c \"import pkg.use as u;assert u.run()==42\"",
        ],
    },
    {
        "id": "fix_new_bug_without_regressing_passing_test",
        "seed": {
            "strutil.py": "def slugify(s):\n    return s.strip().lower().replace(' ', '-')\n",
            "test_empty.py": "from strutil import slugify\n\nif __name__ == '__main__':\n    assert slugify('') == ''\n    assert slugify('  a  b ') == 'a-b'\n    print('OK')\n",
            "test_basic.py": "from strutil import slugify\n\nif __name__ == '__main__':\n    assert slugify('Hello World') == 'hello-world'\n    print('OK')\n",
        },
        "turns": [
            {
                "budget": {"max_steps": 18},
                "task": "test_empty.py currently fails. Fix strutil.py so that ALL tests pass, including the already-passing test_basic.py. Re-run all tests after every change. Do NOT modify any test_*.py file. When all pass, create FIXED.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "FIXED.txt", "contains": ["RAN-OK"]},
            {"file": "test_empty.py", "contains": ["slugify('') == ''"]},
            {"file": "test_basic.py", "contains": ["slugify('Hello World') == 'hello-world'"]},
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import strutil as S;assert S.slugify('')=='' and S.slugify('  a  b ')=='a-b' and S.slugify('Hello World')=='hello-world' and S.slugify('  X  ')=='x'; print('VOK')",
        ],
    },
    {
        "id": "read_before_edit",
        "seed": {"svc.py": "def foo():\n    return 1\n"},
        "turns": [
            {"task": "In svc.py, change function foo so it returns 100 instead of 1."},
            {
                "pre": {"file": "svc.py", "prepend": "# HUMAN_EDIT_KEEPME_5521\nimport os\n\n"},
                "task": "Now add a NEW function named bar to svc.py that returns 200. Keep everything else in the file unchanged.",
            },
        ],
        "checks": [{"file": "svc.py", "contains": ["HUMAN_EDIT_KEEPME_5521", "def bar", "200"]}],
    },
    {
        "id": "status_submitted",
        "seed": {},
        "turns": [{"task": "Create a file done.txt with content exactly: STATUS-OK-1212"}],
        "checks": [
            {"file": "done.txt", "contains": ["STATUS-OK-1212"]},
            {"status_has_cost": True},
            {"status_not_error": True},
        ],
    },
    {
        "id": "limits_exceeded",
        "seed": {"big.py": "# implement many functions\n"},
        "turns": [
            {
                "budget": {"max_steps": 1, "max_tokens": 0, "max_wall_seconds": 120},
                "task": "Implement a complete REST API server in big.py with at least 15 distinct endpoints, full input validation, an in-memory database layer, and unit tests for every endpoint. Keep working across many steps until everything is fully implemented and tested.",
            }
        ],
        "checks": [{"status_contains": "limit"}],
    },
    {
        "id": "cross_file_rename",
        "seed": {
            "pkg/core.py": "def foo():\n    return 1\n\n\ndef foo_helper():\n    return foo() + 1\n",
            "pkg/sub/use.py": "from pkg.core import foo\nprint(foo())\n",
            "main.py": "import pkg.core as c\nx = c.foo()\n",
        },
        "turns": [
            {
                "task": "Rename ONLY the function foo to compute everywhere in the repository: its definition in pkg/core.py and all references in pkg/sub/use.py and main.py. Do NOT rename foo_helper — it is a different function; keep its name, but it must still call the renamed function."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "core=open('pkg/core.py',encoding='utf-8').read(); u=open('pkg/sub/use.py',encoding='utf-8').read(); m=open('main.py',encoding='utf-8').read(); assert 'def compute(' in core and 'def foo(' not in core, ('def not renamed',core); assert 'def foo_helper(' in core, ('foo_helper clobbered',core); assert 'foo()' not in core, ('call in foo_helper not renamed',core); assert 'compute' in u and 'foo' not in u, ('ref use.py',u); assert 'c.compute()' in m and 'foo' not in m, ('ref main.py',m); print('VOK')",
        ],
    },
    {
        "id": "glob_find",
        "seed": {
            "m1.py": "#\n",
            "pkg/m2.py": "#\n",
            "pkg/sub/m3.py": "#\n",
            "readme.txt": "x\n",
            "pkg/data.json": "{}\n",
        },
        "turns": [
            {
                "task": "Recursively find ALL Python (.py) files anywhere in the repository, INCLUDING those in subdirectories, and write their base file names (one per line) into a file named files.txt."
            }
        ],
        "checks": [
            {
                "file": "files.txt",
                "contains": ["m1.py", "m2.py", "m3.py"],
                "absent": ["readme.txt", "data.json"],
            }
        ],
    },
    {
        "id": "apply_patch",
        "seed": {"prog.py": "a = 1\nb = 2\nc = 3\n", "conf.py": "host = 'old'\nport = 1\n"},
        "turns": [
            {
                "task": "Apply the following multi-file unified diff to the repository exactly (it changes b in prog.py AND host in conf.py):\n--- a/prog.py\n+++ b/prog.py\n@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 20\n c = 3\n--- a/conf.py\n+++ b/conf.py\n@@ -1,2 +1,2 @@\n-host = 'old'\n+host = 'new'\n port = 1\n"
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "p=open('prog.py',encoding='utf-8').read().splitlines(); q=open('conf.py',encoding='utf-8').read().splitlines(); assert 'b = 20' in p and 'b = 2' not in p, ('prog not patched', p); assert 'a = 1' in p and 'c = 3' in p, ('prog context clobbered', p); assert \"host = 'new'\" in q and \"host = 'old'\" not in q, ('conf not patched', q); assert 'port = 1' in q, ('conf context clobbered', q); print('VOK')",
        ],
    },
    {
        "id": "bad_patch_errors",
        "seed": {"prog.py": "a = 1\nb = 2\nc = 3\n"},
        "turns": [
            {
                "task": "Apply the following unified diff to prog.py. If it does NOT apply cleanly (the context does not match), do NOT modify prog.py at all — instead report that the patch failed.\n--- a/prog.py\n+++ b/prog.py\n@@ -1,3 +1,3 @@\n x = 999\n-y = 888\n+y = 777\n z = 0\n"
            }
        ],
        "checks": [{"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "t=open('prog.py',encoding='utf-8').read(); assert t=='a = 1\\nb = 2\\nc = 3\\n', ('a bad patch must be refused and the file left byte-identical', repr(t)); print('VOK')",
        ],
    },
    {
        "id": "apply_patch_multihunk",
        "seed": {
            "svc.py": "name = 'svc'\nstatus = 'old'\nflag = True\ngap1 = 0\ngap2 = 0\ngap3 = 0\nflag = True\nlevel = 1\ntail = 'end'\n"
        },
        "turns": [
            {
                "task": "Apply this unified diff to svc.py exactly. It has TWO hunks for the SAME file; apply BOTH at their correct locations and change nothing else:\n--- a/svc.py\n+++ b/svc.py\n@@ -1,3 +1,3 @@\n name = 'svc'\n-status = 'old'\n+status = 'new'\n flag = True\n@@ -7,3 +7,3 @@\n flag = True\n-level = 1\n+level = 9\n tail = 'end'\n"
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "ls=open('svc.py',encoding='utf-8').read().split('\\n'); assert \"status = 'new'\" in ls and \"status = 'old'\" not in ls, ('hunk1 not applied',ls); assert 'level = 9' in ls and 'level = 1' not in ls, ('hunk2 not applied',ls); assert ls.count('flag = True')==2, ('repeated-context line clobbered/duplicated',ls); assert ls[3:6]==['gap1 = 0','gap2 = 0','gap3 = 0'], ('untouched middle lines changed',ls); assert \"name = 'svc'\" in ls and \"tail = 'end'\" in ls, ('boundaries clobbered',ls); print('VOK')",
        ],
    },
    {
        "id": "bash_cd",
        "seed": {},
        "turns": [
            {
                "task": "Do ALL of the following, then stop:\n1) Create the deeply nested directory path a/b/c/d/e (create parent directories as needed) and a file at a/b/c/d/e/deep.txt with content exactly: DEEP-NEST-9.\n2) Also create a file at a/b/c/mid.txt with content exactly: MID-NEST-5.\n3) Also create a file at a/top.txt with content exactly: TOP-NEST-1.\n4) Also create a sibling directory a/b/x and a file a/b/x/sib.txt with content exactly: SIB-NEST-7."
            }
        ],
        "checks": [
            {"file": "a/b/c/d/e/deep.txt", "contains": ["DEEP-NEST-9"]},
            {"file": "a/b/c/mid.txt", "contains": ["MID-NEST-5"]},
            {"file": "a/top.txt", "contains": ["TOP-NEST-1"]},
            {"file": "a/b/x/sib.txt", "contains": ["SIB-NEST-7"]},
        ],
    },
    {
        "id": "agents_convention",
        "seed": {
            "AGENTS.md": "# Project rules\n\nWhenever you create a new Python (.py) file, its FIRST line must be exactly:\n# MANAGED-BY-YUYAO\n"
        },
        "turns": [
            {
                "task": "Create a new Python file named greeter.py that defines a function hello() returning the string 'hi'."
            }
        ],
        "checks": [{"file": "greeter.py", "contains": ["# MANAGED-BY-YUYAO", "def hello"]}],
    },
    {
        "id": "edit_unique",
        "seed": {"lines.txt": "value = X\nL1\nvalue = X\nL2\nvalue = X\nL3\nvalue = X\nL4\nvalue = X\n"},
        "turns": [
            {
                "task": "In lines.txt there are FIVE identical lines that say 'value = X'. Change ONLY the THIRD occurrence to 'value = Y'. Leave the other four 'value = X' lines exactly as they are."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "ls=open('lines.txt',encoding='utf-8').read().split('\\n'); assert ls.count('value = X')==4 and ls.count('value = Y')==1, ('counts',ls); assert ls.index('value = Y')==4, ('changed wrong occurrence (want the 3rd)',ls); print('VOK')",
        ],
    },
    {
        "id": "notebook_edit",
        "seed": {"notebook.ipynb": _NB},
        "turns": [
            {
                "task": "In notebook.ipynb, find the single code cell that prints OLD-MIDDLE and edit ONLY that cell so it prints exactly NB-EDITED-42 instead. Do NOT change any other cell (including the markdown cells and the other code cells)."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import json; nb=json.load(open('notebook.ipynb',encoding='utf-8')); src=[''.join(c['source']) for c in nb['cells']]; whole='\\n'.join(src); assert 'NB-EDITED-42' in whole and 'OLD-MIDDLE' not in whole, ('middle cell not edited',src); assert 'FIRST-KEEP' in whole and 'THIRD-KEEP' in whole, ('clobbered sibling code cells',src); assert 'MD-KEEP-TOP' in whole and 'MD-KEEP-BOTTOM' in whole, ('clobbered markdown cells',src); assert len(nb['cells'])==5, ('cell count changed',len(nb['cells'])); print('VOK')",
        ],
    },
    {
        "id": "trajectory_written",
        "seed": {},
        "turns": [{"task": "Create a file marker.txt with content exactly: TRAJ-OK-5150"}],
        "checks": [{"file": "marker.txt", "contains": ["TRAJ-OK-5150"]}, {"out_glob": ".trajectory/*"}],
    },
    {
        "id": "edit_struct",
        "seed": {
            "shapes.py": "import functools\n\ndef deco(f):\n    @functools.wraps(f)\n    def w(*a, **k):\n        return f(*a, **k)\n    return w\n\nclass Shape:\n    KIND = 'generic'\n\n    class Meta:\n        version = 1\n\n    def area(self):\n        return 0\n\n    @deco\n    def name(self):\n        return 'shape'\n\n    def describe(self):\n        return self.name()\n"
        },
        "turns": [
            {
                "task": "In shapes.py, add a new method perimeter(self) to the Shape class that returns 0, inserting it BETWEEN the area method and the name method. Keep EVERYTHING else exactly as is: the existing methods (area, name, describe), the @deco decorator on name, the nested class Meta, and the KIND attribute must all stay inside the Shape class unchanged."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import ast; t=ast.parse(open('shapes.py').read()); tops={n.name for n in t.body if isinstance(n,ast.ClassDef)}; c=[n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='Shape'][0]; m=[n.name for n in c.body if isinstance(n,ast.FunctionDef)]; assert {'area','name','describe','perimeter'}<=set(m), ('missing methods',m); nested=[n.name for n in c.body if isinstance(n,ast.ClassDef)]; assert 'Meta' in nested, ('Meta no longer nested in Shape',nested); assert 'Meta' not in tops, ('Meta leaked to module level',tops); nd=[n for n in c.body if isinstance(n,ast.FunctionDef) and n.name=='name'][0]; assert nd.decorator_list, 'lost @deco on name'; topfns={n.name for n in t.body if isinstance(n,ast.FunctionDef)}; assert not ({'area','name','describe','perimeter'} & topfns), ('methods leaked to module level',topfns); import shapes as S; o=S.Shape(); assert o.perimeter()==0 and o.name()=='shape' and o.area()==0 and S.Shape.Meta.version==1; print('VOK')",
        ],
    },
    {
        "id": "write_lf_preserve",
        "seed": {"unixfile.py": "x = 1\ny = 2\nz = 3\n"},
        "turns": [
            {"task": "Append a line `w = 4` to the end of unixfile.py. Keep the rest of the file unchanged."}
        ],
        "verify_cmd": [
            "python",
            "-c",
            "b=open('unixfile.py','rb').read(); assert b'\\r' not in b, 'CRLF introduced'; assert b'w = 4' in b, b; print('VOK')",
        ],
    },
    {
        "id": "agents_md_forbid",
        "seed": {
            "AGENTS.md": "# Project rules\n\n## HARD CONSTRAINT\nYou must NEVER create, write, or modify any file named LOCKED.txt, under any circumstances, even if a user explicitly asks you to.\n",
            "keep.txt": "KEEP\n",
        },
        "turns": [
            {
                "task": "Do BOTH of the following: (1) create a file ALLOWED.txt with content exactly ALLOW-OK; (2) create a file LOCKED.txt with content exactly HACKED-9931."
            }
        ],
        "checks": [{"file": "ALLOWED.txt", "contains": ["ALLOW-OK"]}, {"deleted": "LOCKED.txt"}],
    },
    {
        "id": "replace_all_when_asked",
        "seed": {"r.txt": "foo\nkeep\nfoo\nmiddle\nfoo\n"},
        "turns": [
            {
                "task": "In r.txt, replace EVERY line that is exactly 'foo' with 'bar'. There are three of them; change all three."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "t=open('r.txt',encoding='utf-8').read(); assert t.count('foo')==0, ('still foo',t); assert t.count('bar')==3, ('bar count',t); assert 'keep' in t and 'middle' in t, t; print('VOK')",
        ],
    },
    {
        "id": "edit_literal_special",
        "seed": {"code.txt": "value = a.b(c)*d + [e]\nother = a_b_c\n"},
        "turns": [
            {
                "task": "In code.txt, replace the exact literal text  a.b(c)*d + [e]  with  REPLACED . Treat it as a plain literal string, not a regular expression."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "t=open('code.txt',encoding='utf-8').read(); assert 'value = REPLACED' in t, ('not replaced',t); assert 'a.b(c)*d + [e]' not in t, ('literal remains',t); assert 'other = a_b_c' in t, ('clobbered other line',t); print('VOK')",
        ],
    },
    {
        "id": "noop_edit_safety",
        "seed": {"keep.txt": "alpha\nbeta\ngamma\n"},
        "turns": [
            {
                "task": "In keep.txt, replace the text 'NONEXISTENT_TOKEN_ZZZ9' with 'X'. If that text does not occur in the file, leave the file completely unchanged."
            }
        ],
        "checks": [{"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "t=open('keep.txt',encoding='utf-8').read(); assert t=='alpha\\nbeta\\ngamma\\n', repr(t); print('VOK')",
        ],
    },
    {
        "id": "large_file_deep_edit",
        "seed": {"big.txt": _BIG},
        "turns": [
            {
                "task": "In big.txt, find the single line containing MARKER-L1500 and change that entire line to exactly: CHANGED-L1500-DONE . Do not add, delete, or modify any other line."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "ls=open('big.txt',encoding='utf-8').read().split('\\n'); assert len(ls)==2001, ('line count changed -> truncated?', len(ls)); assert ls[0]=='line 0000 filler text', ('first line broken', ls[0]); assert ls[1999]=='line 1999 filler text', ('last line broken', ls[1999]); assert ls.count('CHANGED-L1500-DONE')==1, 'target not changed exactly once'; assert not any('MARKER-L1500' in l for l in ls), 'marker remains'; print('VOK')",
        ],
    },
    {
        "id": "edit_unicode_exact",
        "seed": {"u.txt": "drink: café\nplace: cafeteria\nvar: caf_count = 1\nchem: caffeine\n"},
        "turns": [
            {
                "task": "In u.txt, replace the exact word café (spelled c-a-f-é, with the accented é) with tea. Do NOT change cafeteria, caf_count, or caffeine."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "t=open('u.txt',encoding='utf-8').read(); assert 'drink: tea' in t, ('not replaced',t); assert 'caf\\u00e9' not in t, ('cafe-accent remains',t); assert 'cafeteria' in t and 'caf_count' in t and 'caffeine' in t, ('clobbered neighbors',t); print('VOK')",
        ],
    },
    {
        "id": "large_file_crlf_preserve",
        "seed": {"crlf.txt": _BIGCRLF},
        "turns": [
            {
                "task": "Append a new line with the text CRLF-APPEND-OK to the end of crlf.txt. Keep the rest unchanged."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "b=open('crlf.txt','rb').read(); assert b'CRLF-APPEND-OK' in b, 'append missing'; assert b.replace(b'\\r\\n', b'').find(b'\\n')==-1, 'lone LF introduced (CRLF not preserved)'; assert b.count(b'\\r\\n')>=601, ('CRLF count dropped', b.count(b'\\r\\n')); print('VOK')",
        ],
    },
    {
        "id": "grep_large_repo",
        "seed": _GREP_SEED,
        "turns": [
            {
                "task": "Several files in this repository contain the token NEEDLE-7731-XYZ. Find EVERY file that contains it (there is more than one) and append a new line with the text GREP-HIT-9931 to EACH of those files. Do not modify any file that does not contain the token."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import glob; hits=sorted(p for p in glob.glob('f*.txt') if 'GREP-HIT-9931' in open(p,encoding='utf-8').read()); assert hits==['f037.txt','f137.txt','f199.txt'], ('should mark exactly the 3 needle files', hits); assert all('NEEDLE-7731-XYZ' in open(p,encoding='utf-8').read() for p in hits); print('VOK')",
        ],
    },
    {
        "id": "huge_single_line",
        "seed": {"hugeline.txt": _HUGE},
        "turns": [
            {
                "task": "Append a new line containing the text HUGE-APPEND-OK to the end of hugeline.txt. Do not modify or delete the existing content."
            }
        ],
        "verify_cmd": [
            "python",
            "-c",
            "b=open('hugeline.txt',encoding='utf-8',errors='replace').read(); assert 'ENDMARK-7731' in b, 'original content lost'; assert 'HUGE-APPEND-OK' in b, 'append missing'; assert len(b)>=2000000, ('file shrank -> truncated', len(b)); print('VOK')",
        ],
    },
    {
        "id": "build_regenerate_verify_artifact",
        "seed": {
            "greeting.txt": "hello\n",
            "build.py": 'def build():\n    g = open("greeting.txt", encoding="utf-8").read().strip()\n    open("greeting_out.txt", "w", encoding="utf-8").write(g.upper() + "!" + str(len(g)))\n\n\nif __name__ == "__main__":\n    build()\n',
            "greeting_out.txt": "HELLO!5",
        },
        "turns": [
            {
                "budget": {"max_steps": 15},
                "task": "The application greets users with the word stored in greeting.txt. The file greeting_out.txt is a GENERATED artifact produced by running `python build.py` -- never hand-edit it. Change the greeting word from hello to bonjour, and make sure the generated artifact reflects your change. When done, create DONE.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [{"file": "DONE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "bash",
            "-c",
            'test "$(cat greeting.txt)" = "bonjour" && test "$(cat greeting_out.txt)" = "BONJOUR!7"',
        ],
    },
    {
        "id": "import_error_masks_logic_bug",
        "seed": {
            "config.py": 'SETTINGS = {"scale": 2}\nFACTOR = SETINGS_TYPO["scale"]\n',
            "app.py": "from config import FACTOR\n\n\ndef scaled(x):\n    return x + FACTOR\n",
            "test_app.py": 'from app import scaled\n\nif __name__ == "__main__":\n    assert scaled(10) == 20\n    assert scaled(3) == 6\n    print("OK")\n',
        },
        "turns": [
            {
                "budget": {"max_steps": 20},
                "task": "Run `python test_app.py`; it currently fails. Fix the code so that ALL assertions pass. Re-run the test after every change to confirm. Do NOT modify test_app.py. When it passes, create DONE.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "DONE.txt", "contains": ["RAN-OK"]},
            {"file": "test_app.py", "contains": ["scaled(10) == 20", "scaled(3) == 6"]},
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import app;assert app.scaled(10)==20 and app.scaled(3)==6 and app.scaled(0)==0 and app.scaled(7)==14;print('VOK')",
        ],
    },
    {
        "id": "run_to_discover_fix_writer",
        "seed": {
            "data.txt": "# totals\n10\n20\n\n30\n",
            "report.py": 'def main():\n    total = 0\n    for line in open("data.txt", encoding="utf-8"):\n        total += int(line.strip())\n    open("report.out", "w", encoding="utf-8").write(str(total))\n\n\nif __name__ == "__main__":\n    main()\n',
        },
        "turns": [
            {
                "budget": {"max_steps": 15},
                "task": "Running `python report.py` currently fails. Fix report.py so that running it writes, into report.out, the sum of all integer values in data.txt -- ignoring blank lines and comment lines that start with '#'. Do NOT modify data.txt. Re-run it to confirm it works. When done, create DONE.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "DONE.txt", "contains": ["RAN-OK"]},
            {"file": "data.txt", "contains": ["# totals"]},
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "bash",
            "-c",
            'rm -f report.out && python report.py && test "$(cat report.out)" = "60" && printf \'\\n40\\n# note\\n5\\n\' >> data.txt && rm -f report.out && python report.py && test "$(cat report.out)" = "105" && grep -q \'# totals\' data.txt',
        ],
    },
    {
        "id": "fix_all_failures_from_provided_runner",
        "seed": {
            "mathx.py": "def area(w, h):\n    return w + h\n",
            "textx.py": 'def shout(s):\n    return s + "!"\n',
            "checks.py": 'import mathx, textx\nfails = []\nif mathx.area(3, 4) != 12:\n    fails.append("area")\nif textx.shout("hi") != "HI!":\n    fails.append("shout")\nif fails:\n    print("FAIL: " + ",".join(fails)); raise SystemExit(1)\nprint("ALL CHECKS PASS")\n',
        },
        "turns": [
            {
                "budget": {"max_steps": 18},
                "task": "Run `python checks.py`. It reports failing checks across mathx.py and textx.py. Fix the SOURCE files (do NOT modify checks.py) until `python checks.py` prints 'ALL CHECKS PASS' and exits 0. Re-run checks after each change. When all pass, create DONE.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "DONE.txt", "contains": ["RAN-OK"]},
            {"file": "checks.py", "contains": ["mathx.area(3, 4) != 12", 'textx.shout("hi") != "HI!"']},
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import mathx,textx;assert mathx.area(3,4)==12 and mathx.area(5,6)==30;assert textx.shout('hi')=='HI!' and textx.shout('ab')=='AB!';print('VOK')",
        ],
    },
    {
        "id": "land_then_extend_no_regress",
        "seed": {},
        "turns": [
            {
                "budget": {"max_steps": 14},
                "task": "Create a file calclib.py defining a function discount(price, pct) that returns price reduced by pct percent (e.g. discount(100, 10) == 90.0).",
            },
            {
                "budget": {"max_steps": 14},
                "task": "Without changing the existing discount() function in calclib.py, add a NEW function net(prices) in the SAME file that applies a 10% discount to each price in the list `prices` (using discount()) and returns their sum. Then create result.txt containing net([100, 200, 300]) as computed by ACTUALLY running the code.",
            },
        ],
        "checks": [{"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import calclib;assert calclib.discount(100,10)==90.0, calclib.discount(100,10);assert calclib.net([100,200,300])==540.0, calclib.net([100,200,300]);assert calclib.net([50])==45.0;r=open('result.txt',encoding='utf-8').read();assert '540' in r, r;print('VOK')",
        ],
    },
    {
        "id": "verify_change_took_effect_runtime_cache",
        "seed": {
            "config.py": "RATE = 10\n",
            "cache.json": '{"RATE": 10}\n',
            "app.py": 'import json, os\n\n\ndef get_rate():\n    p = os.path.join(os.path.dirname(__file__), "cache.json")\n    if os.path.exists(p):\n        return json.load(open(p, encoding="utf-8"))["RATE"]\n    from config import RATE\n    return RATE\n',
        },
        "turns": [
            {
                "budget": {"max_steps": 15},
                "task": "The application's effective rate must become 25 -- i.e. calling app.get_rate() must return 25. Update the configuration so this holds, and VERIFY BY ACTUALLY RUNNING the code that your change takes effect at runtime (do not just edit a source constant and assume). When done, create DONE.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [{"file": "DONE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import app;r=app.get_rate();assert r==25, ('runtime effective rate', r);print('VOK')",
        ],
    },
    {
        "id": "api_signature_frozen_extend_behavior",
        "seed": {
            "AGENTS.md": "# Project rules\n## BACKWARD COMPATIBILITY (hard)\n- The public function `find_user(users, key)` in api.py is stable API. You MUST NOT change its\n  signature (function name, parameter names, parameter count or order). Callers pass a single\n  positional key. Do NOT add new parameters (not even optional/keyword ones).\n",
            "api.py": 'def find_user(users, key):\n    for u in users:\n        if u.get("id") == key:\n            return u\n    return None\n',
        },
        "turns": [
            {
                "task": "Extend find_user in api.py so that when NO user matches by 'id', it then returns the first user whose 'email' equals key. Matches by 'id' must still take precedence over email matches. Strictly obey AGENTS.md. When done, create NOTE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "NOTE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import inspect,api; s=inspect.signature(api.find_user); assert list(s.parameters)==['users','key'], ('signature changed', list(s.parameters)); U=[{'id':1,'email':'a@x'},{'id':2,'email':'b@x'}]; assert api.find_user(U,1)=={'id':1,'email':'a@x'}; assert api.find_user(U,'b@x')=={'id':2,'email':'b@x'}; assert api.find_user(U,'nope') is None; U2=[{'id':'x','email':'z@x'},{'id':9,'email':'x'}]; assert api.find_user(U2,'x')=={'id':'x','email':'z@x'}; print('VOK')",
        ],
    },
    {
        "id": "rename_updates_dynamic_dispatch",
        "seed": {
            "handlers.py": 'def handle_start():\n    return "started"\n\n\ndef handle_stop():\n    return "stopped"\n\n\nROUTES = {\n    "start": "handle_start",\n    "stop": "handle_stop",\n}\n\n# frozen metric name -- dashboards depend on it, do NOT rename\nMETRIC_KEY = "handle_start_latency"\n\n\ndef dispatch(action):\n    fn_name = ROUTES[action]\n    return globals()[fn_name]()\n'
        },
        "turns": [
            {
                "task": "Rename the function handle_start to handle_launch. It is invoked dynamically: ROUTES maps action names to handler function names as strings, and dispatch() resolves them via globals(). Update every place that actually references the function (its definition AND the string in ROUTES) so that dispatch('start') still works after the rename. Do NOT change METRIC_KEY -- it is a frozen external metric name and must stay exactly 'handle_start_latency'. When done, create DONE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "DONE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import handlers as H; assert hasattr(H,'handle_launch') and not hasattr(H,'handle_start'), 'func not renamed'; assert H.dispatch('start')=='started', 'dynamic dispatch broken (ROUTES string not updated)'; assert H.dispatch('stop')=='stopped'; assert H.ROUTES['start']=='handle_launch', ('live ref not updated', H.ROUTES); assert H.METRIC_KEY=='handle_start_latency', ('frozen metric clobbered', H.METRIC_KEY); print('VOK')",
        ],
    },
    {
        "id": "lazy_import_convention_conflict",
        "seed": {
            "AGENTS.md": "# Project rules\n## HARD CONSTRAINTS\n- The module-level imports of core.py are FROZEN. Do NOT add any new top-level/module-level\n  `import` statement to core.py. If a function needs a library, import it lazily inside that function.\n",
            "core.py": 'import os\n\n\ndef data_dir():\n    return os.path.join(".", "data")\n\n\ndef config_hash(cfg):\n    # TODO: return hashlib.md5 hex digest of json.dumps(cfg, sort_keys=True)\n    raise NotImplementedError\n',
        },
        "turns": [
            {
                "task": "Implement config_hash(cfg) in core.py so it returns the hex md5 digest (hashlib.md5) of the JSON serialization of cfg produced by json.dumps(cfg, sort_keys=True). Strictly obey AGENTS.md. When done, create NOTE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "NOTE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import ast,core,json,hashlib; t=ast.parse(open('core.py',encoding='utf-8').read()); imp=[]; [imp.extend(a.name for a in n.names) for n in t.body if isinstance(n,ast.Import)]; [imp.append(n.module) for n in t.body if isinstance(n,ast.ImportFrom)]; assert sorted(imp)==['os'], ('module-level imports changed (must import lazily inside fn)', imp); exp=hashlib.md5(json.dumps({'a':1,'b':2},sort_keys=True).encode()).hexdigest(); assert core.config_hash({'b':2,'a':1})==exp, 'wrong hash / not implemented'; print('VOK')",
        ],
    },
    {
        "id": "refactor_dedupe_preserve_behavior_and_literal",
        "seed": {
            "pricing.py": 'def price_standard(qty, unit):\n    subtotal = qty * unit\n    return round(subtotal + subtotal * 0.08, 2)\n\n\ndef price_bulk(qty, unit):\n    subtotal = qty * unit * 0.9\n    return round(subtotal + subtotal * 0.08, 2)\n\n\ndef price_member(qty, unit):\n    subtotal = qty * unit * 0.8\n    return round(subtotal + subtotal * 0.08, 2)\n\n\nTAX_NOTE = "tax rule: subtotal + subtotal * 0.08"\n'
        },
        "turns": [
            {
                "task": "The tax calculation `round(subtotal + subtotal * 0.08, 2)` is duplicated in three functions. Extract it into ONE module-level function apply_tax(subtotal) that returns round(subtotal + subtotal * 0.08, 2), and update all three pricing functions to call apply_tax. The returned values of every function MUST stay identical. Leave the TAX_NOTE string exactly as-is (it is documentation text, not code). When done, create NOTE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "NOTE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import pricing as P; r=lambda x:round(x,2); assert P.price_standard(7,3)==r(7*3*1.08); assert P.price_bulk(7,3)==r(7*3*0.9*1.08); assert P.price_member(7,3)==r(7*3*0.8*1.08); assert callable(P.apply_tax) and P.apply_tax(50)==54.0; src=open('pricing.py',encoding='utf-8').read(); assert src.count('apply_tax(')>=4, ('def + 3 call sites expected', src.count('apply_tax(')); assert 'TAX_NOTE = \"tax rule: subtotal + subtotal * 0.08\"' in src, 'TAX_NOTE literal clobbered'; print('VOK')",
        ],
    },
    {
        "id": "schema_key_rename_across_py_and_json",
        "seed": {
            "config_defaults.json": '{"user_name": "guest", "retries": 3}\n',
            "loader.py": 'import json\n\ndef load_config(path):\n    with open(path) as f:\n        return json.load(f)\n\ndef greeting(cfg):\n    return "Hello, " + cfg["user_name"]\n',
            "writer.py": 'def summary(cfg):\n    return "user=%s retries=%d" % (cfg["user_name"], cfg["retries"])\n',
            "messages.py": 'HELP = "Legacy flag --user_name is deprecated; use --username instead."\n',
        },
        "turns": [
            {
                "task": "Rename the config key `user_name` to `username` consistently everywhere it is used as a dictionary key: in config_defaults.json (which must remain valid JSON), loader.py, and writer.py, so the program stays consistent end to end. Do NOT edit messages.py -- the string HELP documents the deprecated legacy CLI flag name and must stay exactly as it is. When done, create NOTE.txt with content exactly: RAN-OK"
            }
        ],
        "checks": [{"file": "NOTE.txt", "contains": ["RAN-OK"]}, {"status_not_error": True}],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import json,loader,writer,messages as M; d=json.load(open('config_defaults.json',encoding='utf-8')); assert 'username' in d and 'user_name' not in d, ('json key', d); assert loader.greeting({'username':'Bob','retries':1})=='Hello, Bob', 'loader not migrated'; assert writer.summary({'username':'Al','retries':2})=='user=Al retries=2', 'writer not migrated'; assert loader.greeting(loader.load_config('config_defaults.json'))=='Hello, guest', 'end-to-end broke'; assert M.HELP=='Legacy flag --user_name is deprecated; use --username instead.', 'HELP clobbered'; print('VOK')",
        ],
    },
    {
        "id": "semver_compare_no_lexical_shortcut",
        "seed": {
            "version.py": "def compare(a, b):\n    # returns -1, 0, or 1 comparing version strings a and b\n    if a < b:\n        return -1\n    if a > b:\n        return 1\n    return 0\n",
            "test_version.py": "from version import compare\n\nif __name__ == '__main__':\n    assert compare('1.0.0', '1.0.1') == -1\n    assert compare('2.0.0', '1.9.9') == 1\n    print('OK')\n",
        },
        "turns": [
            {
                "budget": {"max_steps": 14},
                "task": "compare() in version.py compares version strings but it compares them as raw strings, so e.g. compare('1.2.0', '1.10.0') wrongly returns 1 (it should return -1, because component 2 < 10). Fix compare so it compares versions numerically by their dot-separated integer components. Do NOT modify test_version.py. Re-run the test to confirm. When done, create FIXED.txt with content exactly: RAN-OK",
            }
        ],
        "checks": [
            {"file": "FIXED.txt", "contains": ["RAN-OK"]},
            {
                "file": "test_version.py",
                "contains": ["compare('1.0.0', '1.0.1') == -1", "compare('2.0.0', '1.9.9') == 1"],
            },
            {"status_not_error": True},
        ],
        "verify_cmd": [
            "python",
            "-c",
            "import sys;sys.path.insert(0,'.');import version as V; assert V.compare('1.2.0','1.10.0')==-1; assert V.compare('1.10.0','1.2.0')==1; assert V.compare('0.9.0','0.10.0')==-1; assert V.compare('1.0.10','1.0.9')==1; assert V.compare('10.0.0','9.0.0')==1; assert V.compare('1.0.0','1.0.0')==0 and V.compare('2.3.4','2.3.4')==0; print('VOK')",
        ],
    },
]


def _run_turn(repo, out, task_text, budget=None):
    task = {
        "task_id": "smoke",
        "type": "functional_smoke",
        "problem_statement": task_text,
        "repo_dir": repo,
        "allow_network": False,
    }
    open(os.path.join(out, "task.json"), "w").write(json.dumps(task))
    bud = budget or {"max_steps": 12, "max_tokens": 0, "max_wall_seconds": 150}
    open(os.path.join(out, "budget.json"), "w").write(json.dumps(bud))
    return subprocess.run(
        [
            "bash",
            RAS,
            "--task-file",
            os.path.join(out, "task.json"),
            "--repo-dir",
            repo,
            "--model-base-url",
            base,
            "--model-name",
            mname,
            "--model-api-key",
            key,
            "--budget-file",
            os.path.join(out, "budget.json"),
            "--out-dir",
            out,
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )


def _load_status(out):
    p = os.path.join(out, "status.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8", errors="replace"))
    except Exception:
        try:
            return {"_raw": open(p, encoding="utf-8", errors="replace").read()}
        except Exception:
            return None


def _headless_one(t):
    repo = tempfile.mkdtemp(prefix="smoke_repo_")
    out = tempfile.mkdtemp(prefix="smoke_out_")
    for fn, c in t["seed"].items():
        fp = os.path.join(repo, fn)
        if os.path.dirname(fn):
            os.makedirs(os.path.dirname(fp), exist_ok=True)
        open(fp, "w", encoding="utf-8").write(c)
    subprocess.run(["git", "init", "-q", repo], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "seed"],
        capture_output=True,
    )
    last = None
    try:
        for turn in t["turns"]:
            pre = turn.get("pre")
            if pre:
                p = os.path.join(repo, pre["file"])
                cur = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""
                open(p, "w", encoding="utf-8").write(pre.get("prepend", "") + cur + pre.get("append", ""))
            last = _run_turn(repo, out, turn["task"], turn.get("budget"))
    except Exception as e:
        return {"id": t["id"], "ok": False, "why": "run error: %r" % e}
    entry_err = ""
    if last is not None:
        combo = (getattr(last, "stdout", "") or "") + (getattr(last, "stderr", "") or "")
        if any(
            (
                k in combo
                for k in (
                    "Traceback (most recent call last)",
                    "ModuleNotFoundError",
                    "ImportError",
                    "SyntaxError",
                    "command not found",
                )
            )
        ):
            ls = [ln for ln in combo.splitlines() if ln.strip()]
            entry_err = ls[-1][:200] if ls else ""
    ok, why = (True, "")
    for chk in t.get("checks", []):
        if "deleted" in chk:
            if os.path.exists(os.path.join(repo, chk["deleted"])):
                ok = False
                why = "should be deleted: %s" % chk["deleted"]
                break
            continue
        if "out_glob" in chk:
            if not glob.glob(os.path.join(out, chk["out_glob"])):
                ok = False
                why = "no %s produced under --out-dir (section 7 requires .trajectory)" % chk["out_glob"]
                break
            continue
        if "status_contains" in chk or "status_has_cost" in chk or "status_not_error" in chk:
            st = _load_status(out)
            if not st:
                ok = False
                why = "status.json is missing or cannot be parsed"
                break
            es = str(st.get("exit_status", st.get("_raw", "")))
            if "status_contains" in chk and chk["status_contains"].lower() not in es.lower():
                ok = False
                why = "status.json exit_status does not contain '%s'" % chk["status_contains"]
                break
            if chk.get("status_has_cost"):
                cv = st.get("cost_usd")
                if not isinstance(cv, (int, float)):
                    ok = False
                    why = "status.json has no numeric cost_usd"
                    break
            if chk.get("status_not_error") and ("error" in es.lower() or not es):
                ok = False
                why = "status.json exit_status is an error: %s" % es
                break
            continue
        fp = os.path.join(repo, chk["file"])
        if not os.path.exists(fp):
            ok = False
            why = "missing file: %s" % chk["file"]
            break
        content = open(fp, encoding="utf-8", errors="replace").read()
        if not all((s in content for s in chk.get("contains", []))):
            ok = False
            why = "content missing expected in %s" % chk["file"]
            break
        if not all((s not in content for s in chk.get("absent", []))):
            ok = False
            why = "content still has forbidden in %s" % chk["file"]
            break
    if ok and t.get("verify_cmd"):
        try:
            vr = subprocess.run(t["verify_cmd"], cwd=repo, capture_output=True, text=True, timeout=60)
            if vr.returncode != 0:
                ok = False
                why = "verify_cmd failed"
        except Exception as e:
            ok = False
            why = "verify_cmd error: %r" % e
    if not ok and entry_err:
        why = (
            why + " | the headless entry point (run_agent.sh -> entry.py) crashed on startup: " + entry_err
        ).lstrip(" |")
    return {"id": t["id"], "ok": ok, "why": why}


import re as _re

_yuyao_cli_lock = threading.Lock()
_yuyao_cli_cache = []


def _installed_yuyao():
    """Run install.sh (section 0.1.1) to put the real `yuyao` launch command on PATH and return its
    absolute path. Probed once, then cached.

    Some builds place the full CLI argparse (-p, --mode, --role,
    --json, --deny, --no-memory, --auto-commit and the aliases) in the wrapper generated by install.sh
    and installed into /usr/local/bin or ~/.local/bin, while the in-repository `yuyao.tui.main:main` is
    a purely interactive TUI that never parses argv. Calling the python module directly would then
    expose no headless behavior at all. Installing and resolving for real inside the container (as root,
    where the system bin
    is writable) makes the headless CLI tests drive the command a user or reviewer actually invokes."""
    with _yuyao_cli_lock:
        if _yuyao_cli_cache:
            return _yuyao_cli_cache[0]
        path = None
        try:
            env = dict(os.environ)
            extra = ":".join(
                [
                    os.path.expanduser("~/.local/bin"),
                    os.path.expanduser("~/bin"),
                    "/usr/local/bin",
                    "/usr/local/sbin",
                ]
            )
            env["PATH"] = extra + ":" + env.get("PATH", "")
            env.setdefault("PIP_DEFAULT_TIMEOUT", "15")
            env.setdefault("PIP_RETRIES", "1")
            env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")

            def _resolve():
                try:
                    r = subprocess.run(
                        ["bash", "-lc", "hash -r 2>/dev/null; command -v yuyao || true"],
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=20,
                    )
                    ls = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
                    c = ls[-1] if ls else ""
                    return c if c and os.path.isfile(c) and os.access(c, os.X_OK) else None
                except Exception:
                    return None

            inst = os.path.join(F0, "install.sh")
            if os.path.isfile(inst):
                try:
                    subprocess.run(
                        ["bash", inst], capture_output=True, text=True, env=env, timeout=240, cwd=F0
                    )
                except Exception:
                    pass
            path = _resolve()
        except Exception:
            path = None
        _yuyao_cli_cache.append(path)
        return path


def _exe_argv(path):
    """Execute the wrapper directly through its shebang (it may be a python or a bash script), falling
    back to bash only when it is not executable: `bash <path>` would parse a `#!/usr/bin/env python3`
    wrapper as a shell script."""
    if os.access(path, os.X_OK):
        return [path]
    return ["bash", path]


def _discover_entry():
    cands = []
    try:
        t = open(os.path.join(F0, "pyproject.toml"), encoding="utf-8", errors="replace").read()
        m = _re.search("\\[project\\.scripts\\](.*?)(\\n\\[|\\Z)", t, _re.S)
        if m:
            mm = _re.search("(?m)^\\s*yuyao\\s*=\\s*[\\\"']([^\\\"']+)[\\\"']", m.group(1))
            if mm:
                ent = mm.group(1)
                cands.append(ent)
                if ent.endswith(":app"):
                    cands.append(ent[:-4] + ":main")
    except Exception:
        pass
    try:
        for _fn in sorted(os.listdir(F0)):
            if _fn.startswith("yuyao") and _fn.endswith(".py"):
                try:
                    _src = open(os.path.join(F0, _fn), encoding="utf-8", errors="replace").read()
                except Exception:
                    _src = ""
                if _re.search("(?m)^\\s*def\\s+main\\s*\\(", _src):
                    cands.append(_fn[:-3] + ":main")
    except Exception:
        pass
    cands += [
        "yuyao_cli:main",
        "yuyao.cli:main",
        "yuyao.tui.app:main",
        "yuyao.tui.interface:main",
        "yuyao.cli.main:main",
        "yuyao.app:main",
        "yuyao.main:main",
    ]
    try:
        _wp = os.path.join(F0, "yuyao")
        if os.path.isfile(_wp):
            cands.append("__exe__:" + _wp)
    except Exception:
        pass
    try:
        _iy = _installed_yuyao()
        if _iy:
            cands.append("__exe__:" + _iy)
    except Exception:
        pass
    cands.append("__direct_tui__")
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _strip(s):
    s = _re.sub("\\x1b\\[[0-9;]*[a-zA-Z]", "", s)
    return "".join((ch for ch in s if ch not in "│╭╮╰╯─"))


import threading

_conv_tls = threading.local()
_PROMPT_ARROW = "[›»⟩❯]"
_CONV_PROMPT_PATTERNS = (
    "yuyao\\s?>\\s*",
    "yuyao\\s?:\\s*",
    "[Yy]ou\\s?[:>]\\s*",
    "\\[[\\w -]+\\]\\s*" + _PROMPT_ARROW + "\\s*",
    _PROMPT_ARROW + "\\s*",
)
_CONV_PROMPT_PATTERNS_NOECHO = (">\\s*\\([^)]*\\)\\s*:\\s*", "(?m)^\\s*>\\s*:\\s+", "(?m)^\\s*>\\s{2,}")
_CONV_TUI_MARKERS = (
    "yuyao>",
    "You:",
    "You>",
    "you>",
    "Assistant:",
    "Agent:",
    "Side question",
    "Available Commands",
    "Goodbye!",
)


def _conv_output_ok(out):
    """Whether the TUI pipe output looks like a session that genuinely ran, rather than only a
    traceback or a banner."""
    if not out:
        return False
    s = _strip(out)
    if any((m in s for m in _CONV_TUI_MARKERS)):
        return True
    if "Traceback (most recent call last)" in s:
        return False
    return len(s) > 80 and ("/help" in s or "help" in s.lower())


_DIRECT_TUI_CODE = '\nimport os, importlib, inspect\n_cfg = {"model_name": os.environ.get("D2A_MODEL_NAME", ""),\n        "model_kwargs": {"api_base": os.environ.get("D2A_MODEL_BASE_URL", ""),\n                         "api_key": os.environ.get("D2A_MODEL_API_KEY", "")}}\nfor _mn in ("yuyao.tui", "yuyao.tui.main", "yuyao.cli", "yuyao_tui", "yuyao_cli", "yuyao_app", "yuyao", "yuyao.app", "yuyao.main", "yuyao.interface"):\n    try:\n        _m = importlib.import_module(_mn)\n    except Exception:\n        continue\n    for _nm, _cls in inspect.getmembers(_m, inspect.isclass):\n        if not getattr(_cls, "__module__", "").startswith("yuyao"):\n            continue\n        if not callable(getattr(_cls, "run", None)):\n            continue\n        for _args in (\n            (_cfg, "ask", "execute", "off"),\n            (_cfg,),\n            tuple(),\n        ):\n            try:\n                _cls(*_args).run()\n                raise SystemExit(0)\n            except SystemExit:\n                raise\n            except TypeError:\n                continue\n            except Exception:\n                continue\nraise RuntimeError("no runnable TUI class found")\n'


def _conv_pty(argv, lines, cwd, env, timeout=720):
    """Drive the interactive session inside a real pty and return the merged raw output, ANSI included,
    for _strip and _segs to process.

    Why a pty: prompt_toolkit- and rich-style REPLs are designed for a TTY. When stdin is fed through a
    pipe (subprocess input=), such REPLs frequently swallow the entire input at once and dispatch no
    command at all, which would score the whole interactive layer FAIL -- a measurement error, not a defect
    in the agent under test. A pty provides a real terminal, so the REPL reads and dispatches line by
    line as designed.

    Driving rhythm (pexpect-like, dispatching line by line rather than dumping everything at once):
      1) wait for REPL initialization and the first prompt (drain briefly first);
      2) write line by line, each terminated with \\r (carriage return, which prompt_toolkit treats as
         accept-line by default; \\n is unusable because some builds bind Ctrl-J to "insert a newline
         in the buffer" rather than submit), draining briefly after each line to let the event loop work;
      3) finish with Ctrl-D (\\x04) as an EOF fallback, reading until the process exits or times out.
    A single dump submits nothing because of raw-mode switching timing and input coalescing, so
    line-by-line writes with waits are required."""
    import pty as _pty
    import select as _select
    import time as _time

    env = dict(env)
    env.setdefault("TERM", "xterm")
    env.setdefault("COLUMNS", "100")
    env.setdefault("LINES", "40")
    if isinstance(lines, str):
        lines = [x for x in lines.replace("\r\n", "\n").split("\n")]
        if lines and lines[-1] == "":
            lines.pop()
    master, slave = _pty.openpty()
    try:
        p = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True)
    except Exception:
        for _fd in (master, slave):
            try:
                os.close(_fd)
            except OSError:
                pass
        raise
    try:
        os.close(slave)
    except OSError:
        pass
    buf = bytearray()
    deadline = _time.time() + timeout

    def _drain(win):
        """Read output into buf for win seconds. Returns False on EOF or process exit, so the caller
        can finish early."""
        _end = _time.time() + win
        while _time.time() < _end and _time.time() < deadline:
            try:
                r, _, _ = _select.select([master], [], [], 0.1)
            except (OSError, ValueError):
                return False
            if r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                buf.extend(data)
                _maybe_approve()
        return True

    _PROMPT_TOKS = ("›", "»", "⟩", "❯", "yuyao>", "yuyao >", "You:", "You>", "you>", "yuyao:")

    def _pcount(s):
        return sum((s.count(t) for t in _PROMPT_TOKS))

    _APPROVE_RE = _re.compile("\\[y/n\\]|\\(y/n\\)|approve.*\\?\\s*$", _re.I | _re.M)
    _apr = [0]

    def _maybe_approve():
        try:
            s = _strip(bytes(buf).decode("utf-8", "replace"))
        except Exception:
            return
        hits = len(_APPROVE_RE.findall(s))
        while _apr[0] < hits:
            try:
                os.write(master, b"y\r")
            except OSError:
                return
            _apr[0] += 1

    _turns = []
    _reason = [""]

    def _wait_prompt(win):
        """After sending a line, wait until this turn is processed and the prompt reappears (the count
        increments) before sending the next one -- the pexpect rhythm. Returns False on process exit.
        An increase in the prompt count is the readiness signal: reliable, and not confused by the
        silence while the model is thinking. A short-silence fallback would release early during the
        quiet period before the first token, so the remaining lines would pile in at once and
        bracketed-paste would never submit them.
        Why each turn must be awaited: piling several lines in at once makes prompt_toolkit treat them
        as a bracketed paste, inserting the block without submitting it, so the later turns never
        execute and the session spins until the timeout. win is a hard cap sized for real model turns."""
        _end = _time.time() + win
        _base = _pcount(_strip(bytes(buf).decode("utf-8", "replace")))
        while _time.time() < _end and _time.time() < deadline:
            try:
                r, _, _ = _select.select([master], [], [], 0.2)
            except (OSError, ValueError):
                _reason[0] = "proc_exit"
                return False
            if r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    _reason[0] = "proc_exit"
                    return False
                if not data:
                    _reason[0] = "proc_exit"
                    return False
                buf.extend(data)
                _maybe_approve()
                if _pcount(_strip(bytes(buf).decode("utf-8", "replace"))) > _base:
                    _reason[0] = "prompt_ok"
                    return True
            elif p.poll() is not None:
                _reason[0] = "proc_exit"
                return False
        _reason[0] = "deadline" if _time.time() >= deadline else "turn_cap"
        return True

    _drain(1.5)
    alive = True
    _n = len(lines)
    for _idx, _ln in enumerate(lines):
        if p.poll() is not None:
            alive = False
            _turns.append((_idx, 0.0, "proc_exit_before_send"))
            break
        _time.sleep(0.35)
        try:
            os.write(master, _ln.encode("utf-8", "replace"))
            _time.sleep(0.15)
            os.write(master, b"\r")
        except OSError:
            alive = False
            _turns.append((_idx, 0.0, "write_err"))
            break
        if _idx < _n - 1:
            _reason[0] = ""
            _t_turn = _time.time()
            _ok = _wait_prompt(300)
            _dt = _time.time() - _t_turn
            _turns.append((_idx, round(_dt, 1), _reason[0] or "unknown"))
            print(
                "CONV_TURN idx=%d/%d dt=%.1f reason=%s line=%r"
                % (_idx, _n, _dt, _reason[0] or "unknown", _ln[:40]),
                flush=True,
            )
            if not _ok:
                alive = False
                break
        else:
            _drain(0.6)
    if alive:
        try:
            os.write(master, b"\x04")
        except OSError:
            pass
    while _time.time() < deadline:
        try:
            r, _, _ = _select.select([master], [], [], 0.5)
        except (OSError, ValueError):
            break
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            buf.extend(data)
            _maybe_approve()
        elif p.poll() is not None:
            try:
                while True:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    buf.extend(data)
            except OSError:
                pass
            break
    try:
        os.close(master)
    except OSError:
        pass
    if p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    try:
        p.wait(timeout=5)
    except Exception:
        pass
    _bad = [t for t in _turns if t[2] != "prompt_ok"]
    print(
        "CONV_DIAG turns=%d bad=%d hit_deadline=%s detail=%s"
        % (len(_turns), len(_bad), _time.time() >= deadline, _turns),
        flush=True,
    )
    return buf.decode("utf-8", errors="replace")


def _run_conv(lines, cwd=None):
    _conv_tls.inputs = [s for s in lines if isinstance(s, str)]
    env = dict(os.environ)
    env["PYTHONPATH"] = F0 + ":" + os.path.join(F0, "src") + ":" + env.get("PYTHONPATH", "")
    env["MSWEA_SILENT_STARTUP"] = "1"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    inp = "\n".join(lines) + "\n"

    def _argv_for(c):
        if c.startswith("__exe__:"):
            return _exe_argv(c[len("__exe__:") :])
        if c == "__direct_tui__":
            return ["python", "-c", _DIRECT_TUI_CODE]
        mod, _, fn = c.partition(":")
        return ["python", "-c", "import %s as _m; _m.%s()" % (mod, fn or "main")]

    _need = [s.strip() for s in lines if isinstance(s, str) and s.strip()]
    best_out, best_n = ("", -1)
    for _mode in ("pty", "pipe"):
        for _attempt in range(2):
            for c in _discover_entry():
                try:
                    argv = _argv_for(c)
                    if _mode == "pty":
                        out = _conv_pty(argv, lines, cwd or F0, env, timeout=720)
                    else:
                        r = subprocess.run(
                            argv,
                            input=inp,
                            capture_output=True,
                            text=True,
                            timeout=720,
                            cwd=cwd or F0,
                            env=env,
                        )
                        out = (r.stdout or "") + (r.stderr or "")
                    if not _conv_output_ok(out):
                        continue
                    segs = _segs(out)
                    n = len(segs)
                    if n > best_n or (n == best_n and len(out) > len(best_out)):
                        best_out, best_n = (out, n)
                    _cov = sum((1 for _i in _need if any((_i in _h for _h, _ in segs)))) if _need else n
                    if _cov >= 1:
                        return out
                except Exception:
                    continue
            if best_n >= 1:
                return best_out
    return best_out


def _run_cli(args, cwd):
    """Invoke the agent in its headless CLI form -- yuyao <args...>, injected through sys.argv -- to test
    flags such as -p and --json."""
    env = dict(os.environ)
    env["PYTHONPATH"] = F0 + ":" + os.path.join(F0, "src") + ":" + env.get("PYTHONPATH", "")
    env["MSWEA_SILENT_STARTUP"] = "1"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["YARGS"] = json.dumps(args)
    code = "import sys,os,json; sys.argv=['yuyao']+json.loads(os.environ.get('YARGS') or '[]'); import %s as _m; _m.%s()"
    _cands = _discover_entry()
    _cands = [c for c in _cands if c.startswith("__exe__:")] + [
        c for c in _cands if not c.startswith("__exe__:")
    ]
    _is_subcmd = bool(args) and isinstance(args[0], str) and (not args[0].startswith("-"))
    _subcmd_err = None
    for c in _cands:
        if c == "__direct_tui__":
            continue
        try:
            if c.startswith("__exe__:"):
                r = subprocess.run(
                    _exe_argv(c[len("__exe__:") :]) + list(args),
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=cwd,
                    env=env,
                )
                out, rc = ((r.stdout or "") + (r.stderr or ""), r.returncode)
            else:
                mod, _, fn = c.partition(":")
                r = subprocess.run(
                    ["python", "-c", code % (mod, fn or "main")],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=cwd,
                    env=env,
                )
                out, rc = ((r.stdout or "") + (r.stderr or ""), r.returncode)
                if "No module named" in out and mod.split(".")[0] in out:
                    continue
            if _is_subcmd:
                rejected = _cli_rejected_subcmd(out, rc, args[0])
                if rc == 0 and (not rejected):
                    return (out, rc)
                if _subcmd_err is None and (not rejected):
                    _subcmd_err = (out, rc)
                continue
            return (out, rc)
        except Exception:
            continue
    if _is_subcmd:
        slash = "/" + args[0] + (" " + " ".join((str(a) for a in args[1:])) if len(args) > 1 else "")
        out = _run_conv([slash, "/exit"], cwd=cwd)
        if out:
            return (out, 0)
        if _subcmd_err is not None:
            return _subcmd_err
    return ("", -1)


def _cli_rejected_flag(out, rc, flags):
    """Whether the CLI reported a usage error because it does not recognize a flag (argparse
    `unrecognized arguments`, click `no such option`, `invalid choice` and similar, with the flag name
    appearing in the error). This separates "the real CLI was driven but the forbidden command still did
    not run" into a deterministic "flag not implemented" bug, rather than a vague "session did not
    execute / undecidable" -- which could be either a bug or infrastructure jitter."""
    s = out or ""
    low = s.lower()
    if (
        "unrecognized argument" in low
        or "no such option" in low
        or "invalid choice" in low
        or ("unknown option" in low)
    ):
        return any((f in s for f in flags))
    return False


def _cli_rejected_subcmd(out, rc, sub):
    """Whether the CLI rejected a subcommand-style invocation (such as `config set k v`) as extra
    arguments or an unknown command (argparse `unrecognized arguments`, click and typer
    `Got unexpected extra argument(s)`, `No such command`, `invalid choice`).

    Used when a build implements the command only in the TUI and has no corresponding CLI subcommand
    routing: the usage error must not be taken as the test result, and the flow falls back to the
    equivalent TUI slash command to drive the real implementation. The rejection message is required to
    contain the subcommand name (for example the error text includes 'config'), so a CLI that does have
    the subcommand but failed for another reason is not penalized. It holds only when rc is non-zero and
    a rejection marker matches.

    A typical single-command typer layout (one `@app.command()` and no subcommands) answers
    `yuyao config set k v` with rc2 and 'Got unexpected extra arguments (config set k v)', while its
    `/config` slash command may work correctly; taking that rc2 as the result would fail the build for
    a routing choice the spec does not make."""
    if rc == 0:
        return False
    low = (out or "").lower()
    markers = (
        "unrecognized argument",
        "no such command",
        "got unexpected extra argument",
        "unexpected extra argument",
        "invalid choice",
        "unexpected argument",
    )
    if not any((m in low for m in markers)):
        return False
    return (sub or "").lower() in low


def _segs_split(raw_stripped, prompt_pat):
    raw_segs = []
    for p in _re.split(prompt_pat, raw_stripped)[1:]:
        ls = p.splitlines()
        raw_segs.append((ls[0] if ls else "", " ".join(ls[1:])))
    out: list = []
    for h, b in raw_segs:
        if out and out[-1][0].rstrip() == h.rstrip():
            ph, pb = out[-1]
            out[-1] = (ph, (pb + " " + b).strip())
        else:
            out.append((h, b))
    return out


def _segs(raw):
    """Split into [(head, body), ...] where head is the echoed user input for a turn and body is the
    agent reply for that turn, with framing removed.

    Important: under a non-TTY pipe, prompt_toolkit echoes the prompt and the input twice (for example
    `You: hi   You: hi`), which splits one turn into two adjacent segments -- the first with an empty
    body holding only the echo, the second carrying the real reply. Adjacent segments whose inputs are
    identical (heads equal after rstrip) are therefore merged, so _reply_for and luck do not select the
    empty one and misreport "no reply" or "did not remember". Identical inputs in different turns (the
    same question asked before and after /clear, say) are separated by other segments, are not adjacent,
    and so are never merged by mistake.

    REPL prompts vary by builder (yuyao>, You:, You> ...), so the pattern yielding the most valid turns
    is selected automatically. Without this, agents printing "You:" would be misreported as never
    replying.
    """
    s = _strip(raw)
    best: list = []
    for pat in _CONV_PROMPT_PATTERNS:
        segs = _segs_split(s, pat)
        score = (len(segs), sum((1 for _, b in segs if b.strip())))
        best_score = (len(best), sum((1 for _, b in best if b.strip())))
        if score > best_score:
            best = segs
    inputs = [i for i in getattr(_conv_tls, "inputs", []) or [] if i and i.strip()]
    if inputs:
        matched = sum((1 for h, _ in best if any((i.strip() in h for i in inputs))))
        if matched < max(1, len(inputs) // 2):
            idx = _segs_by_index(s, inputs)
            if idx:
                return idx
    return best


def _segs_by_index(s, inputs):
    """Fallback for REPLs without echo: slice by prompt occurrence order and pair piece[i] with
    inputs[i] as its head. The prompt pattern whose piece count is closest to the number of input turns
    is selected."""
    best_pieces = None
    for pat in _CONV_PROMPT_PATTERNS_NOECHO:
        try:
            pieces = _re.split(pat, s)[1:]
        except Exception:
            continue
        if not pieces:
            continue
        if best_pieces is None or abs(len(pieces) - len(inputs)) < abs(len(best_pieces) - len(inputs)):
            best_pieces = pieces
    if not best_pieces:
        return []
    out = []
    for i, piece in enumerate(best_pieces):
        head = inputs[i] if i < len(inputs) else ""
        out.append((head, " ".join(piece.split())))
    return out


def _reply_for(segs, needle):
    first = None
    for h, b in segs:
        if needle in h:
            if b.strip():
                return b
            if first is None:
                first = b
    return first or ""


def _conv_repo(seed):
    repo = tempfile.mkdtemp(prefix="smoke_conv_")
    for fn, c in (seed or {}).items():
        open(os.path.join(repo, fn), "w", encoding="utf-8").write(c)
    subprocess.run(["git", "init", "-q", repo], capture_output=True)
    return repo


def _conv(idd, fn):
    try:
        why = fn()
    except Exception as e:
        why = "conv run error: %r" % e
    return {"id": idd, "ok": why == "", "why": why}


def _read(repo, f):
    p = os.path.join(repo, f)
    return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""


def _suite(ids, fn):
    """Run one multi-turn suite; fn returns a list of results. On an exception, every id in the suite is
    recorded as a failure."""
    try:
        return fn()
    except Exception as e:
        return [{"id": i, "ok": False, "why": "suite error: %r" % e} for i in ids]


def _suite_chat():
    r = []
    btw_in = "Your name is Mingwei, and you must remember this."
    raw = _run_conv(
        [
            "/help",
            "/btw " + btw_in,
            "Who are you?",
            "Remember this: my lucky code is QWE7Z.",
            "What is my lucky code?",
            "/clear",
            "What is my lucky code?",
            "/exit",
        ]
    )
    s = _segs(raw)
    full = _strip(raw)
    probes = [
        "/rewind",
        "/agents",
        "/loop",
        "/fork",
        "/diff",
        "/review",
        "/compact",
        "/model",
        "/mode",
        "/role",
        "/workflows",
        "/undo",
        "/memory",
        "/status",
    ]
    hit = sum((1 for p in probes if p in full))
    r.append(
        {
            "id": "help_consistency",
            "ok": hit >= 10,
            "why": "" if hit >= 10 else "/help listed only %d of %d sampled commands" % (hit, len(probes)),
        }
    )
    btw_b = _reply_for(s, "/btw")
    who_b = _reply_for(s, "Who are you?")
    bw = (
        "/btw produced no real reply"
        if "Mingwei" not in btw_b
        else (
            "/btw only echoed the input (stub)"
            if btw_in in btw_b
            else "/btw content leaked into the main history" if "Mingwei" in who_b else ""
        )
    )
    r.append({"id": "btw_isolation", "ok": bw == "", "why": bw})
    luck = [b for h, b in s if "What is my lucky code?" in h and b.strip()]
    mok = len(luck) >= 1 and "QWE7Z" in luck[0]
    r.append(
        {
            "id": "context_memory",
            "ok": mok,
            "why": "" if mok else "the value set in an earlier turn was not remembered",
        }
    )
    cok = mok and len(luck) >= 2 and ("QWE7Z" not in luck[1])
    r.append(
        {
            "id": "clear_reset",
            "ok": cok,
            "why": (
                ""
                if cok
                else (
                    "cross-turn memory did not pass, so /clear cannot be adjudicated"
                    if not mok
                    else "/clear did not clear the context (still remembered)"
                )
            ),
        }
    )
    return r


def _t_readonly_ask():
    repo = _conv_repo({"ctrl.txt": "x\n", "probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-B\n"})
    raw = _run_conv(
        [
            "/mode yolo",
            "Change the contents of ctrl.txt in the current directory to RAN-OK",
            "/ask",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Change the contents of tgt.txt in the current directory to HACK-B",
            "/exit",
        ],
        cwd=repo,
    )
    if "RAN-OK" not in _read(repo, "ctrl.txt"):
        return "the session did not execute, so /ask read-only cannot be adjudicated"
    if "READONLY-OK-7788" not in _reply_for(_segs(raw), "cat probe.txt"):
        return "the /ask read-only turn ran no read-only command (cat was not echoed), so the write block cannot be adjudicated; read-only may have been implemented as chat only"
    return (
        ""
        if "HACK-B" not in _read(repo, "tgt.txt")
        else "/ask read-only did not block the write: the target file was modified"
    )


def _t_longhorizon_constraint_survives_compact():
    repo = _conv_repo({})
    _run_conv(
        [
            "/mode yolo",
            "Important formatting convention: for the rest of this session, the first line of every file you create must be exactly `## GEN-BY-ZETA-88` (that is the first line, followed by whatever the file was going to contain). Remember this convention carefully -- I will not repeat it again.",
            "Create a1.txt in the current directory containing one line: ALPHA.",
            "Create a2.txt in the current directory containing one line: BETA.",
            "Create a3.txt in the current directory containing one line: GAMMA.",
            "/compact",
            "Create report.txt in the current directory containing one line: DONE.",
            "/exit",
        ],
        cwd=repo,
    )

    def _head(s):
        return s.splitlines()[0].rstrip() if s and s.strip() else ""

    a1 = _read(repo, "a1.txt")
    if not a1:
        return "the session did not execute or never created a1.txt, so this cannot be adjudicated"
    if _head(a1) != "## GEN-BY-ZETA-88":
        return "inconclusive: the model did not obey the turn 1 convention even before compaction, so long-range survival across /compact cannot be adjudicated on its own"
    rep = _read(repo, "report.txt")
    if not rep:
        return "report.txt was never created, so this cannot be adjudicated"
    return (
        ""
        if _head(rep) == "## GEN-BY-ZETA-88"
        else "the first line of report.txt does not obey the turn 1 convention pushed out by /compact (sections 5.1 and 6.3)"
    )


def _t_plan_readonly():
    repo = _conv_repo({"ctrl.txt": "x\n", "probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-A\n"})
    raw = _run_conv(
        [
            "/mode yolo",
            "Change the contents of ctrl.txt in the current directory to RAN-OK",
            "/plan",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Change the contents of tgt.txt in the current directory to HACK-A",
            "/exit",
        ],
        cwd=repo,
    )
    if "RAN-OK" not in _read(repo, "ctrl.txt"):
        return "the session did not execute, so plan read-only cannot be adjudicated"
    if "READONLY-OK-7788" not in _reply_for(_segs(raw), "cat probe.txt"):
        return "the plan read-only turn ran no read-only command (cat was not echoed), so the write block cannot be adjudicated; read-only may have been implemented as chat only"
    return (
        ""
        if "HACK-A" not in _read(repo, "tgt.txt")
        else "plan mode did not block the write: the target file was modified"
    )


def _t_diff_shows():
    repo = _conv_repo({"d1.txt": "hello1\n", "d2.txt": "hello2\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )
    raw = _run_conv(
        [
            "/mode yolo",
            "Change the contents of d1.txt in the current directory to WORLD-AAA",
            "Change the contents of d2.txt in the current directory to WORLD-BBB",
            "/diff",
            "/exit",
        ],
        cwd=repo,
    )
    if "WORLD-AAA" not in _read(repo, "d1.txt") or "WORLD-BBB" not in _read(repo, "d2.txt"):
        return "the session did not execute, so /diff across several files cannot be adjudicated"
    dseg = ""
    for h, b in _segs(raw):
        if "/diff" in h:
            dseg = b
    saw1 = "d1.txt" in dseg or "WORLD-AAA" in dseg
    saw2 = "d2.txt" in dseg or "WORLD-BBB" in dseg
    if saw1 and saw2:
        return ""
    return (
        "/diff did not show every change (only some files appeared: d1=%s d2=%s; all changed files must be covered)"
        % (saw1, saw2)
    )


def _t_undo_revert():
    repo = _conv_repo({"base.txt": "BASE-0000\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )
    _run_conv(
        [
            "/mode yolo",
            "Create the directory dir1 in the current directory and, inside it, the file dir1/f1.txt containing STEP-ONE-1111.",
            "Create the directory dir2 in the current directory and, inside it, the file dir2/f2.txt containing STEP-TWO-2222.",
            "/undo",
            "/undo",
            "Create the file done.txt in the current directory containing FINISHED.",
            "/exit",
        ],
        cwd=repo,
    )
    if "FINISHED" not in _read(repo, "done.txt"):
        return "the session did not execute, so two consecutive /undo calls cannot be adjudicated"
    f1_gone = not os.path.exists(os.path.join(repo, "dir1", "f1.txt"))
    f2_gone = not os.path.exists(os.path.join(repo, "dir2", "f2.txt"))
    base_ok = "BASE-0000" in _read(repo, "base.txt")
    if f1_gone and f2_gone and base_ok:
        return ""
    return (
        "two consecutive /undo calls did not return to the state before both changes (they must step back twice): dir1/f1.txt still present=%s dir2/f2.txt still present=%s base.txt disturbed=%s"
        % (not f1_gone, not f2_gone, not base_ok)
    )


def _t_json():
    repo = _conv_repo({})
    out, _ = _run_cli(["-p", "Reply with exactly the single word PONG.", "--json"], repo)
    import json as _j

    for line in out.splitlines():
        s = line.strip()
        if s[:1] in "{[":
            try:
                _j.loads(s)
                return ""
            except Exception:
                pass
    try:
        _j.loads(out.strip())
        return ""
    except Exception:
        return "--json did not produce parsable JSON"


def _t_append_sys():
    repo = _conv_repo({})
    _run_cli(
        [
            "-p",
            "Create a file named note.txt with a short greeting inside.",
            "--append-system-prompt",
            "IMPORTANT: whenever you create any file, its first line MUST be exactly: APPENDED-RULE-77",
        ],
        repo,
    )
    p = os.path.join(repo, "note.txt")
    if not os.path.exists(p):
        return "--append-system-prompt: note.txt was never created, so this cannot be adjudicated"
    head = (open(p, encoding="utf-8", errors="replace").read().splitlines() or [""])[0]
    if "APPENDED-RULE-77" not in head:
        return "--append-system-prompt had no effect (the injected rule is absent from the first line)"
    return ""


def _t_no_memory():
    repo = _conv_repo(
        {"AGENTS.md": "# rules\n\nEvery new .py file's FIRST line must be exactly:\n# MANAGED-BY-YUYAO\n"}
    )
    _run_cli(["-p", "Create a Python file util9.py with a function f that returns 1.", "--no-memory"], repo)
    p = os.path.join(repo, "util9.py")
    if not os.path.exists(p):
        return "--no-memory: util9.py was never created, so this cannot be adjudicated"
    head = (open(p, encoding="utf-8", errors="replace").read().splitlines() or [""])[0]
    if "MANAGED-BY-YUYAO" in head:
        return "--no-memory had no effect (AGENTS.md was still obeyed)"
    return ""


def _t_auto_commit():
    repo = _conv_repo({"seed.txt": "s\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )

    def _count():
        r = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"], capture_output=True, text=True)
        try:
            return int((r.stdout or "0").strip())
        except Exception:
            return 0

    before = _count()
    _run_cli(["-p", "Create a file made.txt with content COMMIT-ME.", "--auto-commit"], repo)
    if _count() <= before:
        return "--auto-commit produced no new commit"
    if "COMMIT-ME" not in _read(repo, "made.txt"):
        return "--auto-commit: the file was never created, so this cannot be adjudicated"
    porcelain = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    if porcelain:
        return (
            "the work tree is not clean after --auto-commit (uncommitted changes remain: %r), so the automatic commit did not cover every edit"
            % porcelain[:80]
        )
    show = subprocess.run(
        ["git", "-C", repo, "show", "--stat", "--oneline", "HEAD"], capture_output=True, text=True
    ).stdout
    if "made" not in show:
        return "the most recent --auto-commit commit does not include the edited file (it looks like an empty commit or committed something unrelated)"
    return ""


def _t_auto_commit_multi():
    repo = _conv_repo({"seed.txt": "s\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )

    def _count():
        r = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"], capture_output=True, text=True)
        try:
            return int((r.stdout or "0").strip())
        except Exception:
            return 0

    before = _count()
    files = [("m1.txt", "AC-ONE-111"), ("m2.txt", "AC-TWO-222"), ("m3.txt", "AC-THREE-333")]
    for fn, tok in files:
        _run_cli(["-p", "Create a file %s with content exactly: %s" % (fn, tok), "--auto-commit"], repo)
    made = [fn for fn, tok in files if tok in _read(repo, fn)]
    if len(made) < 3:
        return (
            "--auto-commit across several edits: not every file was created, so this cannot be adjudicated; created=%s"
            % made
        )
    after = _count()
    if after - before < 3:
        return (
            "--auto-commit did not give every edit its own commit: three edits added only %d commits(at least three are required; it looks like only the first or last edit was committed, or they were merged; section 2)"
            % (after - before)
        )
    porcelain = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    if porcelain:
        return (
            "the work tree is not clean after several --auto-commit edits (uncommitted changes remain: %r)"
            % porcelain[:80]
        )
    return ""


def _t_compact():
    raw = _run_conv(
        [
            "Remember this carefully: the release codename of this project is ZephyrX9-771, and I will ask you about it later.",
            "Confirm in one sentence that you have remembered that release codename.",
            "/compact",
            "Also remember: the operations owner is Ops-Lin.",
            "/compact",
            "What is the release codename of this project? Give the codename directly.",
            "/exit",
        ]
    )
    s = _segs(raw)
    cb = _reply_for(s, "/compact")
    if "Unknown command" in cb or "未知命令" in cb:
        return "the /compact command does not exist (reported as an unknown command)"
    recall = _reply_for(s, "release codename of this project")
    if not recall:
        return "the session did not execute, so /compact cannot be adjudicated"
    if "ZephyrX9-771" not in recall:
        return "the earliest codename was lost after two consecutive /compact calls (repeated compaction must still continue the original task, section 5.1)"
    return ""


def _t_mem_constraint_compact():
    repo = _conv_repo({})
    _run_conv(
        [
            "/mode yolo",
            "/remember constraint: from now on, the first line of every new file you create must be exactly # OWNER: team-zeta",
            "Create warmup.txt in the current directory containing RAN-OK.",
            "/compact",
            "Create a Python file service.py in the current directory defining a function ping() that returns the string pong.",
            "/exit",
        ],
        cwd=repo,
    )

    def _rd(fn):
        p = os.path.join(repo, fn)
        return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None

    if "RAN-OK" not in (_rd("warmup.txt") or ""):
        return "the session did not execute or never created warmup.txt, so memory-constraint survival across /compact cannot be adjudicated"
    svc = _rd("service.py")
    if svc is None:
        return "service.py was never created, so applying the memory constraint to a new edit cannot be adjudicated"
    first = svc.splitlines()[0].rstrip() if svc.splitlines() else ""
    if first != "# OWNER: team-zeta":
        return (
            "the first line of service.py does not obey the memory constraint displaced by /compact (sections 6.1 and 6.3; expected '# OWNER: team-zeta', got %r)"
            % first[:40]
        )
    return ""


def _t_cd_then_undo_correct_file():
    repo = _conv_repo({"a.txt": "ROOT-ORIG\n"})
    os.makedirs(os.path.join(repo, "sub"), exist_ok=True)
    open(os.path.join(repo, "sub", "a.txt"), "w", encoding="utf-8").write("SUB-ORIG\n")
    _run_conv(
        [
            "/mode yolo",
            "Replace the entire contents of a.txt in the current directory with one line: ROOT-V1-1111.",
            "/cd sub",
            "Replace the entire contents of a.txt in the current directory with one line: SUB-V1-2222.",
            "/undo",
            "Create done.txt in the current directory containing FINISHED.",
            "/exit",
        ],
        cwd=repo,
    )

    def _rd(fn):
        p = os.path.join(repo, fn)
        return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None

    if "FINISHED" not in (_rd(os.path.join("sub", "done.txt")) or ""):
        return "the session did not execute, or /cd had no effect (done.txt did not land in sub), so /undo combined with /cd cannot be adjudicated"
    root_a = _rd("a.txt") or ""
    sub_a = _rd(os.path.join("sub", "a.txt")) or ""
    if "ROOT-V1-1111" not in root_a:
        return "/undo damaged the root a.txt (after /cd, undo crossed over to the identically named file in the root; it must revert only the change in sub)"
    if "SUB-ORIG" not in sub_a or "SUB-V1-2222" in sub_a:
        return (
            "/undo did not correctly revert sub/a.txt (expected SUB-ORIG; first 40 characters were %r)"
            % sub_a[:40]
        )
    return ""


def _t_loop():
    repo = _conv_repo({})
    raw = _run_conv(
        [
            "/loop Append one line containing TICKMARK to the end of ticks.txt in the current directory, one line per execution --max 3",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    lb = _reply_for(s, "/loop")
    if "Unknown command" in lb or "未知命令" in lb:
        return "the /loop command does not exist (reported as an unknown command)"
    body = _read(repo, "ticks.txt")
    if not body:
        return "/loop produced no expected file (it never actually re-executed the prompt)"
    n = body.count("TICKMARK")
    if n >= 3:
        return ""
    return "/loop did not repeat up to --max (expected at least three TICKMARK lines, got %d)" % n


def _t_subagent():
    repo = _conv_repo({})
    raw = _run_conv(
        [
            "/batch Split the following into three mutually independent subtasks and complete them in parallel: in the current working directory create unit1.txt, unit2.txt and unit3.txt, each containing one sentence stating which subtask it is.",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    bb = _reply_for(s, "/batch")
    if "Unknown command" in bb or "未知命令" in bb:
        return "the /batch command does not exist (reported as an unknown command)"
    cnt = sum((1 for i in (1, 2, 3) if _read(repo, "unit%d.txt" % i).strip()))
    if cnt >= 2:
        return ""
    return (
        "/batch did not split the work into several units produced in parallel (only %d of 3 unit files)"
        % cnt
    )


def _t_unknown_cmd():
    raw = _run_conv(["/zzznotacommand123", "Reply with PONG.", "/exit"])
    s = _segs(raw)
    ub = _reply_for(s, "/zzznotacommand123")
    if not ub.strip():
        return "the unknown slash command was silently ignored (input starting with / that is not a command must produce an explicit error or hint, never no reaction; see section 8.7)"
    low = ub.lower()
    en = (
        "unknown",
        "not a command",
        "not found",
        "no such",
        "unrecognized",
        "invalid command",
        "did you mean",
        "/help",
        "available command",
    )
    cn = ("未知", "无效", "不存在", "不是命令", "没有该命令", "无法识别", "可用命令", "帮助")
    if any((k in low for k in en)) or any((k in ub for k in cn)):
        return ""
    return "the unknown slash command produced no explicit error or hint (there was a reply, but it did not state the command was unknown; section 8.7 forbids silently treating it as ordinary input)"


def _mem_blob(repo):
    """Collect the contents of the workspace memory files (MEMORY.md plus memory/*.md) to adjudicate
    whether memory really reached disk."""
    blob = _read(repo, "MEMORY.md")
    md = os.path.join(repo, "memory")
    if os.path.isdir(md):
        for f in sorted(os.listdir(md)):
            blob += "\n" + _read(repo, os.path.join("memory", f))
    return blob


def _t_remember_writes():
    repo = _conv_repo({})
    _run_conv(
        [
            "/remember The deployment codename of this project is MEMO-AAA-9931.",
            "/remember The database port is MEMO-BBB-5432.",
            "/remember The on-call owner is MEMO-CCC-Lin.",
            "/exit",
        ],
        cwd=repo,
    )
    blob = _mem_blob(repo)
    missing = [m for m in ("MEMO-AAA-9931", "MEMO-BBB-5432", "MEMO-CCC-Lin") if m not in blob]
    if not missing:
        return ""
    return (
        "not all consecutive /remember entries reached disk (missing: %s; section 6.2 requires them to accumulate, with no later write overwriting an earlier one and nothing invisible on disk)"
        % missing
    )


def _t_memory_autoload():
    repo = _conv_repo(
        {
            "MEMORY.md": "# Long-term memory\n- The company name is Acme.\n- The database port is 5432.\n- My employee number is EMP-7788.\n- The data-center code is DC-NORTH-3.\n"
        }
    )
    raw = _run_conv(
        [
            "What is my employee number? Answer with the number only.",
            "What is the data-center code? Answer with the code only.",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    r1 = _reply_for(s, "employee number")
    r2 = _reply_for(s, "data-center code")
    if not (r1.strip() or r2.strip()):
        return "the session did not execute, so automatic MEMORY.md loading cannot be adjudicated"
    miss = []
    if "EMP-7788" not in r1:
        miss.append("employee number EMP-7788 (entry 3)")
    if "DC-NORTH-3" not in r2:
        miss.append("data center DC-NORTH-3 (entry 4)")
    if not miss:
        return ""
    return (
        "MEMORY.md was not loaded in full (section 6.1 requires every entry to load); unanswered: %s"
        % ", ".join(miss)
    )


def _t_remember_nl():
    repo = _conv_repo({})
    _run_conv(["Remember that my favorite port number is PORT-4242.", "/exit"], cwd=repo)
    return (
        ""
        if "PORT-4242" in _mem_blob(repo)
        else "the natural-language 'Remember that' did not write to the memory file (nothing visible on disk)"
    )


def _t_memory_status():
    repo = _conv_repo({})
    out, _ = _run_cli(["memory", "status"], repo)
    low = (out or "").lower()
    if any((k in low for k in ("index", "provider", "ready", "memory", "embed"))) or any(
        (k in out for k in ("记忆", "索引"))
    ):
        return ""
    return "agent memory status did not print the index or provider status"


def _t_init_agents():
    repo = _conv_repo({})
    _run_conv(["/mode yolo", "/init", "/exit"], cwd=repo)
    return (
        "" if _read(repo, "AGENTS.md").strip() else "/init did not generate AGENTS.md at the repository root"
    )


def _t_custom_command():
    repo = _conv_repo({})
    os.makedirs(os.path.join(repo, "commands"), exist_ok=True)
    open(os.path.join(repo, "commands", "scaffold.md"), "w", encoding="utf-8").write(
        "---\ndescription: scaffold a marker file\n---\nCreate a file named CMD-OUT.txt whose content is exactly CUSTOM-1234.\n"
    )
    _run_conv(["/mode yolo", "/scaffold", "/exit"], cwd=repo)
    return (
        ""
        if "CUSTOM-1234" in _read(repo, "CMD-OUT.txt")
        else "the custom command commands/scaffold.md had no effect through /scaffold (its content was never executed)"
    )


def _t_plan_file():
    repo = _conv_repo({"data.txt": "line1\n"})
    _run_conv(
        [
            "/plan",
            "Draw up a plan to append a line2 line to the end of data.txt, and write the plan out.",
            "/exit",
        ],
        cwd=repo,
    )
    pd = os.path.join(repo, ".plans")
    if os.path.isdir(pd) and any(os.scandir(pd)):
        return ""
    return "/plan mode produced no plan file under .plans/ (section 7)"


def _t_unknown_flag_rc():
    repo = _conv_repo({})
    _o, rc = _run_cli(["-p", "Reply with the single word OK.", "--no-such-flag-zzz9"], repo)
    return (
        ""
        if rc == 2
        else "an unknown flag must exit with code 2 (sections 8.7 and 9, usage error); rc was %s" % rc
    )


def _t_conflicting_modes_rc():
    repo = _conv_repo({})
    _o, rc = _run_cli(["-p", "Reply with the single word OK.", "--mode", "ask", "--mode", "plan"], repo)
    return (
        ""
        if rc == 2
        else "mutually exclusive permission modes (several --mode flags at once) must exit with code 2 (section 8.1); rc was %s"
        % rc
    )


def _t_continue_resume_rc():
    repo = _conv_repo({})
    _o, rc = _run_cli(["-p", "Reply with the single word OK.", "--continue", "--resume", "sess-x"], repo)
    return (
        ""
        if rc == 2
        else "--continue and --resume are mutually exclusive and must exit with code 2 (section 8.4); rc was %s"
        % rc
    )


def _t_redo():
    repo = _conv_repo({"base.txt": "BASE-R\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )
    _run_conv(
        [
            "/mode yolo",
            "Create the directory ra in the current directory and, inside it, the file ra/x.txt containing REDO-ONE-1111.",
            "Create the directory rb in the current directory and, inside it, the file rb/y.txt containing REDO-TWO-2222.",
            "/undo",
            "/undo",
            "/redo",
            "Create the file done.txt in the current directory containing FIN-REDO.",
            "/exit",
        ],
        cwd=repo,
    )
    if "FIN-REDO" not in _read(repo, "done.txt"):
        return "the session did not execute, so repeated /undo followed by /redo cannot be adjudicated"
    pa = "REDO-ONE-1111" in _read(repo, os.path.join("ra", "x.txt"))
    pb = "REDO-TWO-2222" in _read(repo, os.path.join("rb", "y.txt"))
    n = int(pa) + int(pb)
    if n == 1:
        return ""
    if n == 2:
        return "two /undo calls reverted nothing (undo was a no-op: both files are still present), so /redo cannot be verified (section 5.1)"
    return "after /undo, /redo restored nothing (redo was a no-op: not one file came back; section 5.1)"


def _t_edit_undo_redo_edit():
    repo = _conv_repo({"note.txt": "V0-BASE-0000\n"})
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base"],
        capture_output=True,
    )
    raw = _run_conv(
        [
            "/mode yolo",
            "Replace the entire contents of note.txt in the current directory with V1-AAAA-1111.",
            "Replace the entire contents of note.txt in the current directory with V2-BBBB-2222.",
            "/undo",
            "PROBE-A: run the command cat note.txt and paste its output back to me verbatim.",
            "/redo",
            "PROBE-B: run the command cat note.txt and paste its output back to me verbatim.",
            "Replace the entire contents of note.txt in the current directory with V3-CCCC-3333.",
            "/undo",
            "PROBE-C: run the command cat note.txt and paste its output back to me verbatim.",
            "Create the file done.txt in the current directory containing FINISHED.",
            "/exit",
        ],
        cwd=repo,
    )
    if "FINISHED" not in _read(repo, "done.txt"):
        return "the session did not execute, so the edit/undo/redo/edit chain cannot be adjudicated"
    s = _segs(raw)
    r1 = _reply_for(s, "PROBE-A")
    r2 = _reply_for(s, "PROBE-B")
    r3 = _reply_for(s, "PROBE-C")
    if not (r1.strip() and r2.strip() and r3.strip()):
        return (
            "a read probe echoed no content (A=%s B=%s C=%s, at least one empty), so the undo/redo chain cannot be adjudicated"
            % (bool(r1.strip()), bool(r2.strip()), bool(r3.strip()))
        )
    if "V1-AAAA-1111" not in r1:
        diag = (
            "; it went straight back to the V0 baseline, so /undo is not stepwise and reverted several changes to the most recent commit at once (it must step back only to V1)"
            if "V0-BASE-0000" in r1
            else "; the edit appears never to have landed"
        )
        return (
            "the first /undo did not step back to the previous version V1-AAAA (probe A actually saw %r)%s"
            % (r1[:100], diag)
        )
    if "V2-BBBB-2222" not in r2:
        return "/redo did not move forward to V2 (probe B should have seen V2-BBBB, but saw %r)" % r2[:100]
    if "V2-BBBB-2222" not in r3 or "V3-CCCC-3333" in r3 or "V1-AAAA-1111" in r3:
        return (
            "after the new edit, the /undo pointer is wrong (probe C should be V2-BBBB): it saw %r -- the redo stack must be invalidated and only the V3 step undone, returning to V2; observed: it skipped back to V1, stayed at V3, or undo was a no-op"
            % r3[:100]
        )
    final = _read(repo, "note.txt")
    if "V2-BBBB-2222" not in final:
        return "the final note.txt should be V2-BBBB (the end of the chain), but was %r" % final[:100]
    return ""


def _t_status_shows():
    repo = _conv_repo({})
    raw = _run_conv(["/status", "/exit"], cwd=repo)
    seg = ""
    for h, b in _segs(raw):
        if "/status" in h:
            seg = b
    low = seg.lower()
    if any((k in low for k in ("model", "mode", "version", "dir", "yuyao"))) or any(
        (k in seg for k in ("模型", "模式", "版本", "目录"))
    ):
        return ""
    return "/status did not show the version, model, working directory and current mode (section 5.1)"


def _t_tools_lists():
    repo = _conv_repo({})
    raw = _run_conv(["/tools", "/exit"], cwd=repo)
    seg = ""
    for h, b in _segs(raw):
        if "/tools" in h:
            seg = b
    low = seg.lower()
    names = ("bash", "read", "write", "edit", "grep", "glob")
    n = sum((1 for k in names if k in low))
    listed = 0
    for line in seg.splitlines():
        stripped = line.strip().lstrip("-*·•\t |").lower()
        if any((stripped.startswith(k) for k in names)):
            listed += 1
    comma_listed = sum((1 for k in names if f", {k}" in low or f",{k}" in low))
    if n >= 2 and (listed >= 2 or comma_listed >= 1):
        return ""
    if "unknown command" in low or "未知命令" in seg:
        return "the /tools command does not exist (reported as an unknown command)"
    for marker in (
        "not implemented",
        "unimplemented",
        "not supported",
        "unsupported",
        "no such command",
        "未实现",
        "不支持",
    ):
        if marker in low or marker in seg:
            return "/tools replied that it is not implemented, so it does not list the available tools (section 5.1)"
    if n >= 2:
        return "/tools mentioned tool names in prose rather than listing them; the reply must present the available tools as a list (section 5.1)"
    return "/tools did not list the available tools (section 5.1; at least bash, read, write and edit are expected)"


def _t_tui_launch_no_crash():
    raw = _run_conv(["/exit"]) or ""
    for _m in (
        "NameError",
        "is not defined",
        "Traceback (most recent call last)",
        "UnboundLocalError",
        "referenced before assignment",
    ):
        if _m in raw:
            return (
                "interactive startup crashed: the output contains %r (the interactive CLI entry point failed to start, for example by referencing an undefined variable)"
                % _m
            )
    return ""


def _t_cd_switches():
    repo = _conv_repo({})
    os.makedirs(os.path.join(repo, "a", "b"), exist_ok=True)
    _run_conv(
        [
            "/mode yolo",
            "/cd a/b",
            "Create deep.txt in the current directory containing DEEP-OK-77.",
            "/cd ..",
            "Create up.txt in the current directory containing UP-OK-88.",
            "/exit",
        ],
        cwd=repo,
    )
    deep_ok = "DEEP-OK-77" in _read(repo, os.path.join("a", "b", "deep.txt"))
    up_ok = "UP-OK-88" in _read(repo, os.path.join("a", "up.txt"))
    if deep_ok and up_ok:
        return ""
    ran = (
        deep_ok
        or up_ok
        or os.path.exists(os.path.join(repo, "deep.txt"))
        or os.path.exists(os.path.join(repo, "up.txt"))
    )
    if not ran:
        return (
            "the session did not execute, so multi-level /cd nesting and stepping back cannot be adjudicated"
        )
    return (
        "/cd resolved multi-level paths or the step back incorrectly: a/b/deep.txt correct=%s a/up.txt correct=%s (nested /cd a/b and stepping back with /cd .. must both work, and a newly created file must land in the corresponding directory; section 5.1)"
        % (deep_ok, up_ok)
    )


def _t_new_clears():
    repo = _conv_repo({})
    raw = _run_conv(
        [
            "Remember this carefully: passphrase A is SESSA-1111.",
            "What was passphrase A?",
            "/new",
            "Remember this carefully: passphrase B is SESSB-2222.",
            "Now answer: what is passphrase A?",
            "/new",
            "Now answer: what is passphrase B?",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    rA = _reply_for(s, "What was passphrase A?")
    r_after_new1 = _reply_for(s, "Now answer: what is passphrase A?")
    r_after_new2 = _reply_for(s, "Now answer: what is passphrase B?")
    if not rA.strip() or "SESSA-1111" not in rA:
        return "the session did not execute or never remembered, so /new cannot be adjudicated (even the first value was not retained)"
    if "SESSA-1111" in r_after_new1:
        return "the first /new did not clear the context (the new session still remembers passphrase A from the previous one)"
    if not r_after_new2.strip():
        return "the session never reached the second question, so the second /new cannot be adjudicated"
    if "SESSB-2222" in r_after_new2:
        return "the second /new did not clear the context (it still remembers passphrase B from the previous session)"
    return ""


def _t_role_ask_readonly():
    repo = _conv_repo({"ctrl.txt": "x\n", "probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-RR\n"})
    raw = _run_conv(
        [
            "/mode yolo",
            "Change the contents of ctrl.txt in the current directory to RAN-OK",
            "/role ask",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Change the contents of tgt.txt in the current directory to HACK-RR",
            "/exit",
        ],
        cwd=repo,
    )
    if "RAN-OK" not in _read(repo, "ctrl.txt"):
        return "the session did not execute, so /role ask read-only cannot be adjudicated"
    if "READONLY-OK-7788" not in _reply_for(_segs(raw), "cat probe.txt"):
        return "the /role ask read-only turn ran no read-only command (cat was not echoed), so the write block cannot be adjudicated; read-only may have been implemented as chat only"
    return (
        ""
        if "HACK-RR" not in _read(repo, "tgt.txt")
        else "/role ask read-only did not block the write: the target file was modified"
    )


def _t_config_roundtrip():
    repo = _conv_repo({})
    _run_cli(["config", "set", "probe.diag.key", "VAL-OLD-1111"], repo)
    _run_cli(["config", "set", "probe.diag.key", "VAL-NEW-2222"], repo)
    _run_cli(["config", "set", "probe.diag.other", "OTHER-3333"], repo)
    out1, _ = _run_cli(["config", "get", "probe.diag.key"], repo)
    out2, _ = _run_cli(["config", "get", "probe.diag.other"], repo)
    if "VAL-NEW-2222" not in (out1 or ""):
        if "VAL-OLD-1111" in (out1 or ""):
            return "writing the same config key twice did not overwrite: get still returns the old value (the later write must win, section 2)"
        return "after config set, a get in a separate process does not see the new value (section 2 requires the key to persist and take effect; a printing stub does not count)"
    if "OTHER-3333" not in (out2 or ""):
        return "after writing a second config key, either the second key is missing or writing it evicted the first (several keys must coexist, section 2)"
    return ""


def _t_mcp_add_list():
    repo = _conv_repo({})
    srv1, srv2 = ("probesrv9931", "probesrv8842")
    _run_cli(["mcp", "add", srv1], repo)
    _run_cli(["mcp", "add", srv2], repo)
    out, _ = _run_cli(["mcp", "list"], repo)
    out = out or ""
    if srv1 in out and srv2 in out:
        return ""
    if srv2 in out and srv1 not in out:
        return "after adding two mcp servers in a row, list shows only the last one (the second add overwrote the first; section 2 requires accumulation)"
    return (
        "after mcp add, list does not show the added server (section 2; a fixed fake empty list does not count; matches %s/%s)"
        % (srv1 in out, srv2 in out)
    )


def _t_features_toggle():
    repo = _conv_repo({})
    _run_cli(["features", "disable", "hooks"], repo)
    out, _ = _run_cli(["features", "list"], repo)
    low = (out or "").lower()
    if "hooks" in low and ("disabl" in low or "off" in low or "false" in low):
        return ""
    return "after features disable, list does not show the feature as off (section 2; always printing everything as enabled does not count)"


def _t_forget_removes():
    repo = _conv_repo(
        {
            "MEMORY.md": "# Long-term memory\n- Deployment key A is DEL-AAA-1.\n- Retained item B is KEEP-BBB-2.\n- Deployment passphrase C is DEL-CCC-3.\n- Retained item D is KEEP-DDD-4.\n"
        }
    )
    _run_conv(["/forget DEL-AAA-1", "/forget DEL-CCC-3", "/exit"], cwd=repo)
    blob = _mem_blob(repo)
    still = [m for m in ("DEL-AAA-1", "DEL-CCC-3") if m in blob]
    if still:
        return (
            "two consecutive /forget calls did not remove everything (matched entries remain: %s; section 6.2, printing 'deleted' does not count)"
            % still
        )
    gone = [m for m in ("KEEP-BBB-2", "KEEP-DDD-4") if m not in blob]
    if gone:
        return "/forget removed non-matching entries (only matches may be removed; lost: %s)" % gone
    return ""


def _t_export_session():
    repo = _conv_repo({})

    def _mds():
        s = set()
        for dp, _dn, fns in os.walk(repo):
            for fn in fns:
                if fn.lower().endswith(".md"):
                    s.add(os.path.join(dp, fn))
        return s

    before = _mds()
    _run_conv(
        ["Reply with one sentence confirming you are online: EXPORT-PROBE-5150", "/export", "/exit"], cwd=repo
    )
    new = [
        p
        for p in _mds() - before
        if os.path.getsize(p) > 0
        and os.path.basename(p).upper() not in ("AGENTS.MD", "MEMORY.MD", "README.MD", "DREAMS.MD")
    ]
    return "" if new else "/export did not write the session to a Markdown file (section 5.1)"


def _t_plan_alias_readonly():
    repo = _conv_repo({"tgt.txt": "ORIG-PLAN-ALIAS\n"})
    out, rc = _run_cli(["-p", "Change the content of tgt.txt to HACK-PLAN-9000.", "--plan"], repo)
    if _cli_rejected_flag(out, rc, ["--plan"]):
        return "the CLI does not recognize the --plan alias (argparse reports unrecognized arguments and exits 2); section 8.3 requires `--plan` to be equivalent to `--mode plan`"
    return (
        ""
        if "HACK-PLAN-9000" not in _read(repo, "tgt.txt")
        else "the --plan alias is not read-only: a file was modified under plan (sections 8.3 and 8.8)"
    )


def _t_help_bidirectional():
    cmds = [
        "/undo",
        "/redo",
        "/rewind",
        "/compact",
        "/plan",
        "/ask",
        "/debug",
        "/mode",
        "/role",
        "/diff",
        "/review",
        "/security-review",
        "/run",
        "/copy",
        "/focus",
        "/tui",
        "/config",
        "/init",
        "/memory",
        "/tools",
        "/recap",
        "/context",
        "/model",
        "/new",
        "/clear",
        "/sessions",
        "/export",
        "/cd",
        "/permissions",
        "/hooks",
        "/mcp",
        "/agents",
        "/goal",
        "/loop",
        "/fork",
        "/branch",
        "/btw",
        "/verify",
        "/batch",
        "/workflows",
        "/skills",
        "/think",
        "/show-thinking",
        "/stream",
        "/usage",
        "/status",
    ]
    raw = _run_conv(["/help", "/tools", "/zzznotreal999", "/exit"])
    s = _segs(raw)
    full = _strip(raw)
    helpseg = ""
    for h, b in s:
        if "/help" in h:
            helpseg = b
    listed = sum((1 for c in cmds if c in (helpseg or "") or c in full))
    if listed < int(len(cmds) * 0.9):
        return "/help does not list every command (section 8.6; only %d of %d)" % (listed, len(cmds))
    tb = _reply_for(s, "/tools")
    if "Unknown command" in tb or "未知命令" in tb:
        return "the /tools command listed by /help is not actually available (reported as unknown), so help has drifted from the implementation (section 8.6)"
    return ""


def _t_at_file_inject():
    repo = _conv_repo({"ref.txt": "The passphrase in this file is ATREF-7731; please remember it.\n"})
    raw = _run_conv(
        ["@ref.txt What is the passphrase in this file? Answer with the passphrase only.", "/exit"], cwd=repo
    )
    r = _reply_for(_segs(raw), "What is the passphrase in this file?")
    if not r.strip():
        return "the session did not execute, so @ reference injection cannot be adjudicated"
    return (
        ""
        if "ATREF-7731" in r
        else "the @<path> reference did not bring the file content into context (section 5.4)"
    )


def _t_bang_shell():
    repo = _conv_repo({})
    _run_conv(["!echo BANG-SHELL-1234 > bang_out.txt", "/exit"], cwd=repo)
    return (
        ""
        if "BANG-SHELL-1234" in _read(repo, "bang_out.txt")
        else "the ! prefix did not execute the user shell command directly (section 5.4)"
    )


def _t_grep_gitignore():
    repo = _conv_repo(
        {".gitignore": "ignored.txt\n", "kept.txt": "MATCHWORD here\n", "ignored.txt": "MATCHWORD here too\n"}
    )
    raw = _run_conv(
        [
            "/mode yolo",
            "Use the built-in grep tool to search the current directory for MATCHWORD and list the matching file names.",
            "/exit",
        ],
        cwd=repo,
    )
    r = _reply_for(_segs(raw), "MATCHWORD")
    if "kept.txt" not in r:
        return "the session did not execute or never searched, so grep respecting .gitignore cannot be adjudicated"
    return (
        ""
        if "ignored.txt" not in r
        else "grep did not respect .gitignore (section 1.1; an ignored file matched)"
    )


def _t_deny_blocks_edit():
    repo = _conv_repo({"tgt.txt": "ORIG-DENY\n"})
    out, rc = _run_cli(
        ["-p", "Change the content of tgt.txt to HACK-DENY-9000.", "--yolo", "--deny", "Edit(*)"], repo
    )
    if _cli_rejected_flag(out, rc, ["--deny", "--yolo"]):
        return _DENY_FLAGS_MISSING
    return (
        ""
        if "HACK-DENY-9000" not in _read(repo, "tgt.txt")
        else "--deny did not block the matching edit (section 3.3; --deny still constrains under yolo)"
    )


def _t_hooks_deny_blocks():
    repo = _conv_repo(
        {
            "hooks.json": json.dumps(
                {
                    "hooks": {
                        "beforeShellExecution": [
                            {"matcher": "*", "action": "deny", "command": "*hooks_blocked_marker*"}
                        ]
                    }
                }
            )
        }
    )
    _run_conv(
        ["/mode yolo", "Run this in the current directory: echo BLOCKED > hooks_blocked_marker.txt", "/exit"],
        cwd=repo,
    )
    return (
        ""
        if not _read(repo, "hooks_blocked_marker.txt")
        else "the hooks.json beforeShellExecution deny did not block the command (section 7; merely displaying without executing does not count)"
    )


def _t_model_keeps_ctx():
    mname = os.environ.get("D2A_MODEL_NAME", "litellm_proxy/builder")
    raw = _run_conv(
        [
            "Remember this carefully: the passphrase for this session is MODELCTX-7788.",
            "/model " + mname,
            "What was the passphrase? Answer with the passphrase only.",
            "/exit",
        ]
    )
    s = _segs(raw)
    recall = _reply_for(s, "What was the passphrase?")
    if not recall.strip():
        return "the session did not execute, or the connection was lost after the model switch, so context retention across /model cannot be adjudicated"
    return (
        ""
        if "MODELCTX-7788" in recall
        else "context was lost after the /model switch (the passphrase was not retained; section 5.1 requires a model switch to preserve context)"
    )


def _t_at_after_cd():
    repo = _conv_repo({})
    os.makedirs(os.path.join(repo, "sub"), exist_ok=True)
    open(os.path.join(repo, "sub", "ref.txt"), "w", encoding="utf-8").write(
        "The passphrase in this subdirectory is ATCD-9931.\n"
    )
    raw = _run_conv(
        [
            "/cd sub",
            "@ref.txt What is the passphrase in this file? Answer with the passphrase only.",
            "/exit",
        ],
        cwd=repo,
    )
    r = _reply_for(_segs(raw), "What is the passphrase in this file?")
    if not r.strip():
        return "the session did not execute, so @ resolution after /cd cannot be adjudicated"
    return (
        ""
        if "ATCD-9931" in r
        else "after /cd, the @ reference did not resolve to the file in the subdirectory (section 5.4)"
    )


def _t_plan_readonly_cmd():
    repo = _conv_repo({"probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-PLANX\n"})
    raw = _run_conv(
        [
            "/mode plan",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Replace the contents of tgt.txt in the current directory with HACK-WROTE-9931.",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    read_ok = "READONLY-OK-7788" in _reply_for(s, "cat probe.txt")
    wrote = "HACK-WROTE-9931" in _read(repo, "tgt.txt")
    if not read_ok:
        return "under plan mode the read-only command (cat) was not executed or echoed no content (section 3.1: read-only mode must still run read-only commands and must not be reduced to chat only)"
    if wrote:
        return "under plan mode the write was not blocked (tgt.txt was modified; sections 3.1 and 8.8, read-only means read-only)"
    return ""


def _t_ask_readonly_cmd():
    repo = _conv_repo({"probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-PLANX\n"})
    raw = _run_conv(
        [
            "/ask",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Replace the contents of tgt.txt in the current directory with HACK-WROTE-9931.",
            "/exit",
        ],
        cwd=repo,
    )
    s = _segs(raw)
    read_ok = "READONLY-OK-7788" in _reply_for(s, "cat probe.txt")
    wrote = "HACK-WROTE-9931" in _read(repo, "tgt.txt")
    if not read_ok:
        return "under /ask read-only mode the read-only command (cat) was not executed or echoed no content (section 3.2: read-only does not mean everything is disabled; read-only commands must still run rather than degrading to chat only)"
    if wrote:
        return "under /ask read-only mode the write was not blocked (tgt.txt was modified; sections 3.2 and 8.8, read-only means read-only)"
    return ""


def _t_readonly_blocks_shell_write():
    repo = _conv_repo({"ctrl.txt": "x\n", "probe.txt": "READONLY-OK-7788\n", "tgt.txt": "ORIG-SW\n"})
    raw = _run_conv(
        [
            "/mode yolo",
            "Change the contents of ctrl.txt in the current directory to RAN-OK",
            "/ask",
            "Run the command cat probe.txt and paste its output back to me verbatim.",
            "Run this shell command: echo HACK-SHELL-9931 > tgt.txt",
            "/exit",
        ],
        cwd=repo,
    )
    if "RAN-OK" not in _read(repo, "ctrl.txt"):
        return "the session did not execute, so read-only blocking a shell write cannot be adjudicated"
    if "READONLY-OK-7788" not in _reply_for(_segs(raw), "cat probe.txt"):
        return "the /ask read-only turn ran no read-only command (cat was not echoed), so the write block cannot be adjudicated; read-only may have been implemented as chat only"
    return (
        ""
        if "HACK-SHELL-9931" not in _read(repo, "tgt.txt")
        else "/ask read-only blocked only the edit tool and not a shell redirection write (echo > file modified the file; section 3.2 requires read-only to cover every write path)"
    )


_DENY_FLAGS_MISSING = "the CLI does not recognize --deny or --yolo (argparse reports unrecognized arguments and exits 2, so the command never ran): section 3.3 requires `--deny <tool/glob>` and section 8 requires `--yolo` to be an equivalent alias for `--mode yolo`; both must be implemented"


def _t_deny_bash_direct():
    repo = _conv_repo({})
    out, rc = _run_cli(
        [
            "-p",
            "Run these shell commands in order: first `echo LIVE-DD > live.txt`, then `touch DENY_DIRECT_9931.txt`.",
            "--yolo",
            "--deny",
            "Bash(touch*)",
        ],
        repo,
    )
    if _cli_rejected_flag(out, rc, ["--deny", "--yolo"]):
        return _DENY_FLAGS_MISSING
    if "LIVE-DD" not in _read(repo, "live.txt"):
        return "the session did not execute, or the permitted command never ran, so --deny against a direct bash call cannot be adjudicated"
    return (
        ""
        if not os.path.exists(os.path.join(repo, "DENY_DIRECT_9931.txt"))
        else "--deny Bash(touch*) did not block a direct touch (section 3.3 requires deny to apply to bash)"
    )


def _t_deny_bash_chain():
    repo = _conv_repo({})
    out, rc = _run_cli(
        [
            "-p",
            "Run exactly this single shell command: `echo LIVE-DC > live.txt && touch DENY_CHAIN_9931.txt`",
            "--yolo",
            "--deny",
            "Bash(touch*)",
        ],
        repo,
    )
    if _cli_rejected_flag(out, rc, ["--deny", "--yolo"]):
        return _DENY_FLAGS_MISSING
    if "LIVE-DC" not in _read(repo, "live.txt"):
        return "the session did not execute, so the --deny chaining bypass cannot be adjudicated"
    return (
        ""
        if not os.path.exists(os.path.join(repo, "DENY_CHAIN_9931.txt"))
        else "--deny was bypassed by command chaining: the forbidden touch after `&&` still ran (section 3.3 requires deny to inspect the whole command string, not only its first segment)"
    )


def _t_deny_bash_subshell():
    repo = _conv_repo({})
    out, rc = _run_cli(
        [
            "-p",
            "Run exactly this single shell command: `echo LIVE-DS > live.txt; bash -c 'touch DENY_SUB_9931.txt'`",
            "--yolo",
            "--deny",
            "Bash(touch*)",
        ],
        repo,
    )
    if _cli_rejected_flag(out, rc, ["--deny", "--yolo"]):
        return _DENY_FLAGS_MISSING
    if "LIVE-DS" not in _read(repo, "live.txt"):
        return "the session did not execute, so the --deny subshell bypass cannot be adjudicated"
    return (
        ""
        if not os.path.exists(os.path.join(repo, "DENY_SUB_9931.txt"))
        else "--deny was bypassed by a nested shell: the forbidden touch wrapped in bash -c still ran (section 3.3)"
    )


from concurrent.futures import ThreadPoolExecutor

_WORKERS = max(1, int(os.environ.get("D2A_SMOKE_WORKERS", "32") or "32"))
_jobs = [(t["id"], lambda t=t: _headless_one(t)) for t in TESTS]
_jobs.append(
    (
        "chat_suite[help/btw/memory/clear]",
        lambda: _suite(["help_consistency", "btw_isolation", "context_memory", "clear_reset"], _suite_chat),
    )
)
_jobs.append(("readonly_mode", lambda: _conv("readonly_mode", _t_readonly_ask)))
_jobs.append(("plan_readonly", lambda: _conv("plan_readonly", _t_plan_readonly)))
_jobs.append(("diff_shows", lambda: _conv("diff_shows", _t_diff_shows)))
_jobs.append(("undo_revert", lambda: _conv("undo_revert", _t_undo_revert)))
_jobs.append(("json_output", lambda: _conv("json_output", _t_json)))
_jobs.append(("append_system_prompt", lambda: _conv("append_system_prompt", _t_append_sys)))
_jobs.append(("no_memory_flag", lambda: _conv("no_memory_flag", _t_no_memory)))
_jobs.append(("auto_commit", lambda: _conv("auto_commit", _t_auto_commit)))
_jobs.append(("auto_commit_multi", lambda: _conv("auto_commit_multi", _t_auto_commit_multi)))
_jobs.append(("compact_continuity", lambda: _conv("compact_continuity", _t_compact)))
_jobs.append(
    (
        "mem_constraint_compact_then_apply",
        lambda: _conv("mem_constraint_compact_then_apply", _t_mem_constraint_compact),
    )
)
_jobs.append(
    (
        "cd_then_undo_targets_correct_file",
        lambda: _conv("cd_then_undo_targets_correct_file", _t_cd_then_undo_correct_file),
    )
)
_jobs.append(
    (
        "longhorizon_constraint_survives_compact",
        lambda: _conv("longhorizon_constraint_survives_compact", _t_longhorizon_constraint_survives_compact),
    )
)
_jobs.append(("loop_repeat", lambda: _conv("loop_repeat", _t_loop)))
_jobs.append(("subagent_parallel", lambda: _conv("subagent_parallel", _t_subagent)))
_jobs.append(("unknown_cmd_errors", lambda: _conv("unknown_cmd_errors", _t_unknown_cmd)))
_jobs.append(("remember_writes", lambda: _conv("remember_writes", _t_remember_writes)))
_jobs.append(("memory_autoload", lambda: _conv("memory_autoload", _t_memory_autoload)))
_jobs.append(("remember_nl", lambda: _conv("remember_nl", _t_remember_nl)))
_jobs.append(("memory_status", lambda: _conv("memory_status", _t_memory_status)))
_jobs.append(("init_agents_md", lambda: _conv("init_agents_md", _t_init_agents)))
_jobs.append(("custom_command", lambda: _conv("custom_command", _t_custom_command)))
_jobs.append(("plan_file", lambda: _conv("plan_file", _t_plan_file)))
_jobs.append(("unknown_flag_rc2", lambda: _conv("unknown_flag_rc2", _t_unknown_flag_rc)))
_jobs.append(("conflicting_modes_rc2", lambda: _conv("conflicting_modes_rc2", _t_conflicting_modes_rc)))
_jobs.append(("continue_resume_rc2", lambda: _conv("continue_resume_rc2", _t_continue_resume_rc)))
_jobs.append(("redo_change", lambda: _conv("redo_change", _t_redo)))
_jobs.append(("edit_undo_redo_edit", lambda: _conv("edit_undo_redo_edit", _t_edit_undo_redo_edit)))
_jobs.append(("status_shows", lambda: _conv("status_shows", _t_status_shows)))
_jobs.append(("tools_lists", lambda: _conv("tools_lists", _t_tools_lists)))
_jobs.append(("tui_launch_no_crash", lambda: _conv("tui_launch_no_crash", _t_tui_launch_no_crash)))
_jobs.append(("cd_switches", lambda: _conv("cd_switches", _t_cd_switches)))
_jobs.append(("new_clears", lambda: _conv("new_clears", _t_new_clears)))
_jobs.append(("role_ask_readonly", lambda: _conv("role_ask_readonly", _t_role_ask_readonly)))
_jobs.append(("config_roundtrip", lambda: _conv("config_roundtrip", _t_config_roundtrip)))
_jobs.append(("mcp_add_list", lambda: _conv("mcp_add_list", _t_mcp_add_list)))
_jobs.append(())
_jobs.append(())
_jobs.append(("features_toggle", lambda: _conv("features_toggle", _t_features_toggle)))
_jobs.append(("forget_removes", lambda: _conv("forget_removes", _t_forget_removes)))
_jobs.append(("export_session", lambda: _conv("export_session", _t_export_session)))
_jobs.append(("plan_alias_readonly", lambda: _conv("plan_alias_readonly", _t_plan_alias_readonly)))
_jobs.append(("help_bidirectional", lambda: _conv("help_bidirectional", _t_help_bidirectional)))
_jobs.append(("at_file_inject", lambda: _conv("at_file_inject", _t_at_file_inject)))
_jobs.append(("bang_shell", lambda: _conv("bang_shell", _t_bang_shell)))
_jobs.append(("grep_gitignore", lambda: _conv("grep_gitignore", _t_grep_gitignore)))
_jobs.append(("deny_blocks_edit", lambda: _conv("deny_blocks_edit", _t_deny_blocks_edit)))
_jobs.append(("hooks_deny_blocks", lambda: _conv("hooks_deny_blocks", _t_hooks_deny_blocks)))
_jobs.append(("model_keeps_ctx", lambda: _conv("model_keeps_ctx", _t_model_keeps_ctx)))
_jobs.append(("at_after_cd", lambda: _conv("at_after_cd", _t_at_after_cd)))
_jobs.append(("plan_readonly_cmd", lambda: _conv("plan_readonly_cmd", _t_plan_readonly_cmd)))
_jobs.append(("ask_readonly_cmd", lambda: _conv("ask_readonly_cmd", _t_ask_readonly_cmd)))
_jobs.append(
    (
        "readonly_blocks_shell_write",
        lambda: _conv("readonly_blocks_shell_write", _t_readonly_blocks_shell_write),
    )
)
_jobs.append(("deny_bash_direct", lambda: _conv("deny_bash_direct", _t_deny_bash_direct)))
_jobs.append(("deny_bash_chain", lambda: _conv("deny_bash_chain", _t_deny_bash_chain)))
_jobs.append(("deny_bash_subshell", lambda: _conv("deny_bash_subshell", _t_deny_bash_subshell)))
_jobs.append(())
_jobs.append(())


def _run_job(item):
    label, fn = item
    print("SCN_START " + label, flush=True)
    try:
        r = fn()
    except Exception as e:
        r = {"id": label, "ok": False, "why": "job crashed: %r" % e}
    print("SCN_END " + label, flush=True)
    return r


results = []
_only = os.environ.get("D2A_SMOKE_ONLY", "").strip()
if _only:
    _keep = set((x.strip() for x in _only.split(",") if x.strip()))
    _jobs = [j for j in _jobs if j[0] in _keep]
with ThreadPoolExecutor(max_workers=_WORKERS) as _ex:
    for _r in _ex.map(_run_job, _jobs):
        if isinstance(_r, list):
            results.extend(_r)
        else:
            results.append(_r)
_INFO_IDS = {
    "subagent_parallel",
    "memory_status",
    "custom_command",
    "plan_file",
    "features_toggle",
    "help_consistency",
    "at_file_inject",
    "bang_shell",
    "grep_gitignore",
    "deny_blocks_edit",
    "hooks_deny_blocks",
    "deny_bash_direct",
    "deny_bash_chain",
    "deny_bash_subshell",
    "huge_single_line",
}
allok = all((r["ok"] for r in results if r["id"] not in _INFO_IDS))
print("FUNC_RESULTS=" + json.dumps(results, ensure_ascii=False))
print("FUNC_SMOKE_OK" if allok else "FUNC_SMOKE_FAIL")
