#!/usr/bin/env python3
"""Compile a small structured stack-machine language (.stk) into a Chef recipe.

    python3 gen/chef.py json2dir.stk > json2dir.chef

Source language (one statement per line, '#' starts a comment; the same
language as json2dir-shakespeare's gen/play.py):

    vars a b c            scalar variables           -> ingredients
    stacks S T            stacks                     -> mixing bowls
    proc name { ... }     procedure; `main` is the entry point

    x = expr              + - * / % ( ) ints 'c' names  (/ and % by constants)
    push S expr / pop S x
    getc x                next byte of stdin, -1 at the end  (Take x from refrigerator)
    putc expr / out "text"  append to the output
    call name / return
    if cond { } elif cond { } else { }
    while cond { }  loop { }  break  continue
    halt                  serve the output and stop
    fail "message"        serve "json2dir: message" (and nothing else) and stop

    cond: expr (== != < > <= >=) expr, combined with and / or / not / ( )

Chef has no comparisons, no jumps and no output before the end, so:

  * Arithmetic runs on the top of a mixing bowl ("Put", "Add", "Remove",
    "Combine", "Divide", "Fold").  Conditions are reduced to "x is nonzero" or
    "x is zero": a != b is a - b, a == b is "a - b is zero", `and`/`or` multiply
    or add squares.  a < b uses floating point: 1 + floor((b - a - 1) / 2^32),
    where floor is rounding with the 1.5 * 2^52 trick (Acme::Chef divides in
    Perl floating point).
  * A loop "Verb the x. ... Verb until verbed." runs while x is nonzero; an if
    is such a loop that clears its flag.  Every loop needs its own verb.
  * Code with break/continue/return or recursive calls is cut into blocks.
    Each block is a loop on its own guard ingredient; a jump sets the target's
    guard.  The blocks sit, in groups with a group guard, inside one endless
    loop.  Calls of recursive procs push a return number on a bowl; procs that
    are not recursive are inlined.
  * The output is collected in a bowl (with a -1 at the bottom), reversed into
    another bowl at the end, liquefied, poured into the baking dish and served
    with "Refrigerate for 1 hour".  `fail` serves only its message instead.

Acme::Chef reads one number per line with "Take ... from refrigerator"; the
launcher feeds one decimal byte per line and the line 256 at the end.
"""
import re
import sys

# ---------------------------------------------------------------- tokenizer / parser

class Error(Exception):
    pass


TOKEN = re.compile(r"""
    (?P<ws>[ \t\r]+|\#[^\n]*)
  | (?P<nl>\n|;)
  | (?P<num>\d+)
  | (?P<chr>'(?:\\.|\\x[0-9a-fA-F]{2}|[^\\'])')
  | (?P<str>"(?:\\.|[^\\"])*")
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>==|!=|<=|>=|[-+*/%()<>={}])
""", re.X)

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}


def unescape(body):
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1]
            if nxt == "x":
                out.append(chr(int(body[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(ESCAPES[nxt])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(text):
    toks, pos, line = [], 0, 1
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise Error(f"line {line}: bad character {text[pos]!r}")
        kind, val = m.lastgroup, m.group()
        if kind == "nl":
            toks.append(("nl", None, line))
        elif kind == "num":
            toks.append(("num", int(val), line))
        elif kind == "chr":
            toks.append(("num", ord(unescape(val[1:-1])), line))
        elif kind == "str":
            toks.append(("str", unescape(val[1:-1]), line))
        elif kind in ("name", "op"):
            toks.append((kind, val, line))
        line += val.count("\n")
        pos = m.end()
    toks.append(("eof", None, line))
    return toks


class Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind, val=None):
        t = self.next()
        if t[0] != kind or (val is not None and t[1] != val):
            raise Error(f"line {t[2]}: expected {val or kind}, got {t[1]!r}")
        return t

    def skip_nl(self):
        while self.peek()[0] == "nl":
            self.next()

    def program(self):
        vars_, stacks, procs = [], [], {}
        while True:
            self.skip_nl()
            t = self.next()
            if t[0] == "eof":
                return vars_, stacks, procs
            if t[1] in ("vars", "stacks"):
                while self.peek()[0] == "name":
                    (vars_ if t[1] == "vars" else stacks).append(self.next()[1])
            elif t[1] == "proc":
                name = self.expect("name")[1]
                procs[name] = self.block()
            else:
                raise Error(f"line {t[2]}: unexpected {t[1]!r}")

    def block(self):
        self.expect("op", "{")
        body = []
        while True:
            self.skip_nl()
            if self.peek()[1] == "}" and self.peek()[0] == "op":
                self.next()
                return body
            body.append(self.statement())

    def statement(self):
        t = self.next()
        line, word = t[2], t[1]
        if t[0] == "name" and self.peek()[1] == "=" and self.peek()[0] == "op":
            self.next()
            return ("assign", line, word, self.expr())
        if word == "push":
            s = self.expect("name")[1]
            return ("push", line, s, self.expr())
        if word == "pop":
            s = self.expect("name")[1]
            return ("pop", line, s, self.expect("name")[1])
        if word == "getc":
            return ("getc", line, self.expect("name")[1])
        if word == "putc":
            return ("putc", line, self.expr())
        if word == "out":
            return ("out", line, self.expect("str")[1])
        if word == "call":
            return ("call", line, self.expect("name")[1])
        if word == "fail":
            return ("fail", line, self.expect("str")[1])
        if word in ("return", "break", "continue", "halt"):
            return (word, line)
        if word == "if":
            arms = [(self.cond(), self.block())]
            els = None
            while self.peek()[1] in ("elif", "else"):
                if self.next()[1] == "elif":
                    arms.append((self.cond(), self.block()))
                else:
                    els = self.block()
                    break
            return ("if", line, arms, els)
        if word == "while":
            return ("while", line, self.cond(), self.block())
        if word == "loop":
            return ("while", line, None, self.block())
        raise Error(f"line {line}: unknown statement {word!r}")

    def cond(self):
        left = self.cond_and()
        while self.peek()[1] == "or":
            self.next()
            left = ("or", left, self.cond_and())
        return left

    def cond_and(self):
        left = self.cond_not()
        while self.peek()[1] == "and":
            self.next()
            left = ("and", left, self.cond_not())
        return left

    def cond_not(self):
        if self.peek()[1] == "not":
            self.next()
            return ("not", self.cond_not())
        if self.peek()[1] == "(":
            save = self.i
            try:
                self.next()
                c = self.cond()
                self.expect("op", ")")
                if self.peek()[1] not in ("==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%"):
                    return c
            except Error:
                pass
            self.i = save
        a = self.expr()
        op = self.next()
        if op[1] not in ("==", "!=", "<", ">", "<=", ">="):
            raise Error(f"line {op[2]}: expected a comparison, got {op[1]!r}")
        return ("cmp", op[1], a, self.expr())

    def expr(self):
        left = self.term()
        while self.peek()[1] in ("+", "-") and self.peek()[0] == "op":
            op = self.next()[1]
            left = (op, left, self.term())
        return left

    def term(self):
        left = self.factor()
        while self.peek()[1] in ("*", "/", "%") and self.peek()[0] == "op":
            op = self.next()[1]
            left = (op, left, self.factor())
        return left

    def factor(self):
        t = self.next()
        if t[0] == "num":
            return ("num", t[1])
        if t[0] == "name":
            return ("var", t[1])
        if t[1] == "-":
            f = self.factor()
            if f[0] == "num":
                return ("num", -f[1])
            return ("-", ("num", 0), f)
        if t[1] == "(":
            e = self.expr()
            self.expect("op", ")")
            return e
        raise Error(f"line {t[2]}: bad expression at {t[1]!r}")


# ---------------------------------------------------------------- conditions

M = 2 ** 32                 # |a - b| must stay below this for < > <= >=
MAGIC = 3 * 2 ** 51         # (x + MAGIC) - MAGIC rounds x to an integer


def atom(e):
    return e[0] in ("num", "var")


def nonneg(e):
    if e[0] == "num":
        return e[1] >= 0
    if e[0] in ("ge0", "gt0", "isz", "nz01", "sq"):
        return True
    if e[0] in ("+", "*"):
        return nonneg(e[1]) and nonneg(e[2])
    return False


def exact01(e):
    if e[0] in ("ge0", "gt0", "isz", "nz01"):
        return True
    if e[0] == "*":
        return exact01(e[1]) and exact01(e[2])
    return False


def sub(a, b):
    if b == ("num", 0):
        return a
    if a[0] == "num" and b[0] == "num":
        return ("num", a[1] - b[1])
    return ("-", a, b)


def norm(c):
    """A condition as ("nz", e) (true iff e != 0) or ("z", e) (true iff e == 0)."""
    k = c[0]
    if k == "cmp":
        op, a, b = c[1], c[2], c[3]
        if op == "!=":
            return ("nz", sub(a, b))
        if op == "==":
            return ("z", sub(a, b))
        if op == "<":
            return ("nz", ("gt0", sub(b, a)))
        if op == ">":
            return ("nz", ("gt0", sub(a, b)))
        if op == "<=":
            return ("nz", ("ge0", sub(b, a)))
        return ("nz", ("ge0", sub(a, b)))
    if k == "not":
        kind, e = norm(c[1])
        return ("z" if kind == "nz" else "nz", e)
    a, b = norm(c[1]), norm(c[2])
    if k == "and":
        if a[0] == "z" and b[0] == "z":
            return ("z", ("+", sq(a[1]), sq(b[1])))
        return ("nz", ("*", as_nz(a), as_nz(b)))
    # or
    if a[0] == "z" and b[0] == "z":
        return ("z", ("*", a[1], b[1]))
    x, y = as_nz(a), as_nz(b)
    if nonneg(x) and nonneg(y):
        return ("nz", ("+", x, y))
    return ("nz", ("+", sq(x), sq(y)))


def sq(e):
    return e if exact01(e) else ("sq", e)


def as_nz(c):
    return c[1] if c[0] == "nz" else ("isz", c[1])


# ---------------------------------------------------------------- names

NOUNS = """almonds apples apricots bananas basil bay-leaves beetroot blackberries
blueberries bread brioche butter buttermilk cardamom carrots cashews celery cheddar
cherries chestnuts chickpeas chillies chives chocolate cinnamon cloves cocoa coconut
coffee cornflour courgettes cranberries cream crumbs cucumber cumin currants custard
dates dill eggs fennel figs flour garlic ginger gooseberries grapes hazelnuts honey
jam leeks lemons lentils limes macadamias mangoes maple-syrup marjoram marzipan
meringue milk mint molasses mushrooms mustard nutmeg oats olives onions oranges
oregano paprika parsley parsnips peaches peanuts pears peas pecans pepper pineapple
pistachios plums polenta pomegranate potatoes prunes pumpkin quinces radishes raisins
raspberries rhubarb rice rosemary saffron sage semolina shallots sorrel spinach
strawberries sugar sultanas sweetcorn tarragon thyme tomatoes treacle turmeric
vanilla walnuts watercress yoghurt""".split()

ADJECTIVES = """fresh dried toasted ground candied roasted chopped grated whipped melted
crushed sliced frozen wild sweet salted spiced golden dark white red green black
creamy crispy smoked soft ripe young aged warm cold flaked minced diced halved
blanched glazed poached stewed brown caster icing""".split()

VERBS = """whisk simmer boil roast toast grill poach steam knead sift blend mash bake
sear braise glaze baste brush chill cool heat warm melt soften season salt sweeten
caramelise marinate infuse reduce thicken strain drain rinse peel slice grate crush
pound roll shape layer sprinkle dust coat stuff cover rest prove cream fluff toss
turn scrape skim ladle spoon pipe garnish plate taste check watch pinch crimp score
carve zest juice squeeze press smoke cure pickle brine scald temper stew saute
flambe blanch parboil deglaze render macerate frost""".split()

ADVERBS = """gently slowly briskly lightly firmly quickly evenly carefully patiently
swiftly softly thoroughly delicately vigorously lovingly steadily calmly neatly
boldly finely roughly loosely tightly warmly coolly re pre half double hand slow
quick twice over under pan oven spoon fork batch hot cold dry wet long short
""".split()


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


class Names:
    def __init__(self):
        self.ingredients = []          # (key, name, value)
        self.by_key = {}
        self.pool = [f"{a} {n}" for n in NOUNS for a in ADJECTIVES]
        # spread the names so neighbouring ingredients differ
        step = 37
        while len(self.pool) % step == 0:
            step += 2
        self.pool = [self.pool[(i * step) % len(self.pool)] for i in range(len(self.pool))]
        self.next_name = 0
        verbs = [v for v in VERBS]
        self.verbs = verbs + [f"{a}-{v}" for a in ADVERBS for v in verbs]
        self.next_verb = 0

    def ing(self, key, value=0):
        if key not in self.by_key:
            if self.next_name >= len(self.pool):
                raise Error("out of ingredient names")
            name = self.pool[self.next_name]
            self.next_name += 1
            self.by_key[key] = name
            self.ingredients.append((key, name, value))
        return self.by_key[key]

    def verb(self):
        if self.next_verb >= len(self.verbs):
            raise Error("out of verbs")
        v = self.verbs[self.next_verb]
        self.next_verb += 1
        return v


def past(v):
    return v + "d" if v.endswith("e") else v + "ed"


# ---------------------------------------------------------------- compiler

class Compiler:
    def __init__(self, vars_, stacks, procs):
        self.vars, self.stacks, self.procs = vars_, stacks, procs
        if "main" not in procs:
            raise Error("no proc main")
        self.n = Names()
        self.bowl = {}
        for i, s in enumerate(stacks + ["__out", "__rev", "__call"]):
            self.bowl[s] = i + 2
        self.lines = []          # (indent, text)
        self.depth = 0
        self.recursive = self.find_recursive()
        self.nlabels = 0
        self.inits = []          # negative constants: computed at the start

    # ------------------------------------------------ helpers

    def find_recursive(self):
        calls = {p: set() for p in self.procs}

        def scan(body, p):
            for s in body:
                if s[0] == "call":
                    if s[2] not in self.procs:
                        raise Error(f"line {s[1]}: unknown proc {s[2]!r}")
                    calls[p].add(s[2])
                elif s[0] == "if":
                    for _, b in s[2]:
                        scan(b, p)
                    if s[3]:
                        scan(s[3], p)
                elif s[0] == "while":
                    scan(s[3], p)
        for p, b in self.procs.items():
            scan(b, p)
        rec = set()
        for p in self.procs:
            seen, todo = set(), list(calls[p])
            while todo:
                q = todo.pop()
                if q == p:
                    rec.add(p)
                    break
                if q not in seen:
                    seen.add(q)
                    todo.extend(calls[q])
        return rec

    def var(self, name):
        if name not in self.vars:
            raise Error(f"unknown variable {name!r}")
        return self.n.ing(("var", name), 0)

    def const(self, k):
        if k < 0:
            key = ("const", k)
            if key not in self.n.by_key:
                name = self.n.ing(key, 0)
                self.inits.append((name, -k))
            return self.n.by_key[key]
        return self.n.ing(("const", k), k)

    def tmp(self, k):
        return self.n.ing(("tmp", k), 0)

    def ctl(self, d, k):
        return self.n.ing(("ctl", d, k), 0)

    def say(self, text):
        self.lines.append((self.depth, text + "."))

    def bname(self, b):
        return "the mixing bowl" if b == 1 else f"the {ordinal(b)} mixing bowl"

    def put(self, x, b=1):
        self.say(f"Put the {x} into {self.bname(b)}")

    def fold(self, x, b=1):
        self.say(f"Fold the {x} into {self.bname(b)}")

    def arith(self, op, x, b=1):
        if op == "+":
            self.say(f"Add the {x} to {self.bname(b)}")
        elif op == "-":
            self.say(f"Remove the {x} from {self.bname(b)}")
        elif op == "*":
            self.say(f"Combine the {x} into {self.bname(b)}")
        else:
            self.say(f"Divide the {x} into {self.bname(b)}")

    def set_to(self, x, value_ing):
        self.put(value_ing)
        self.fold(x)

    def open_loop(self, ing):
        v = self.n.verb()
        self.say(f"{v.capitalize()} the {ing}")
        self.depth += 1
        return v

    def close_loop(self, v, dec=None):
        self.depth -= 1
        if dec:
            self.say(f"{v.capitalize()} the {dec} until {past(v)}")
        else:
            self.say(f"{v.capitalize()} until {past(v)}")

    # ------------------------------------------------ expressions

    def atom_ing(self, e):
        return self.const(e[1]) if e[0] == "num" else self.var(e[1])

    def gen(self, e, b, k):
        """Leave the value of e on top of bowl b, using temporaries k, k+1, ..."""
        op = e[0]
        if atom(e):
            self.put(self.atom_ing(e), b)
        elif op in ("+", "-", "*"):
            l, r = e[1], e[2]
            if atom(r):
                self.gen(l, b, k)
                self.arith(op, self.atom_ing(r), b)
            elif op in ("+", "*") and atom(l):
                self.gen(r, b, k)
                self.arith(op, self.atom_ing(l), b)
            else:
                self.gen(r, b, k)
                self.fold(self.tmp(k), b)
                self.gen(l, b, k + 1)
                self.arith(op, self.tmp(k), b)
        elif op in ("/", "%"):
            d = e[2]
            if d[0] != "num" or d[1] <= 0:
                raise Error("/ and % need a positive constant divisor")
            d = d[1]
            if op == "/":
                self.gen(e[1], b, k)
                self.floordiv(d, b)
            else:
                self.gen(e[1], b, k)
                self.fold(self.tmp(k), b)
                self.put(self.tmp(k), b)
                self.floordiv(d, b)
                self.arith("*", self.const(d), b)
                self.fold(self.tmp(k + 1), b)
                self.put(self.tmp(k), b)
                self.arith("-", self.tmp(k + 1), b)
        elif op in ("ge0", "gt0"):
            # [x >= 0] = 1 + floor(x / M);  [x > 0] = [x - 1 >= 0]
            # floor(x / M) = round((2x - M + 1) / 2M) for |x| < M
            self.gen(e[1], b, k)
            self.arith("*", self.const(2), b)
            self.arith("-", self.const(M - 1 if op == "ge0" else M + 1), b)
            self.arith("/", self.const(2 * M), b)
            self.arith("+", self.const(MAGIC), b)
            self.arith("-", self.const(MAGIC), b)
            self.arith("+", self.const(1), b)
        elif op == "sq":
            self.gen(e[1], b, k)
            self.fold(self.tmp(k), b)
            self.put(self.tmp(k), b)
            self.arith("*", self.tmp(k), b)
        elif op in ("isz", "nz01"):
            self.gen(e[1], b, k)
            self.fold(self.tmp(k), b)
            first, then = (1, 0) if op == "isz" else (0, 1)
            self.put(self.const(first), b)
            self.fold(self.tmp(k + 1), b)
            v = self.open_loop(self.tmp(k))
            self.put(self.const(then), b)
            self.fold(self.tmp(k + 1), b)
            self.put(self.const(0), b)
            self.fold(self.tmp(k), b)
            self.close_loop(v)
            self.put(self.tmp(k + 1), b)
        else:
            raise Error(f"bad expression {e!r}")

    def floordiv(self, d, b):
        # floor(x / d) = round((2x - d + 1) / 2d)
        if d == 1:
            return
        self.arith("*", self.const(2), b)
        self.arith("-", self.const(d - 1), b)
        self.arith("/", self.const(2 * d), b)
        self.arith("+", self.const(MAGIC), b)
        self.arith("-", self.const(MAGIC), b)

    # ------------------------------------------------ simple (native) statements

    def is_simple(self, s, inproc=()):
        k = s[0]
        if k in ("return", "break", "continue"):
            return False
        if k == "call":
            p = s[2]
            if p in self.recursive:
                return False
            return all(self.is_simple(x) for x in self.procs[p])
        if k == "if":
            return all(self.is_simple(x) for _, b in s[2] for x in b) and \
                all(self.is_simple(x) for x in (s[3] or []))
        if k == "while":
            return s[2] is not None and all(self.is_simple(x) for x in s[3])
        return True

    def native(self, s, d):
        """Chef steps for a simple statement; d is the control nesting depth."""
        k = s[0]
        if k == "assign":
            self.gen(s[3], 1, 0)
            self.fold(self.var(s[2]))
        elif k == "push":
            self.gen(s[3], self.stack(s[2]), 0)
        elif k == "pop":
            self.fold(self.var(s[3]), self.stack(s[2]))
        elif k == "getc":
            x = self.var(s[2])
            self.say(f"Take {x} from refrigerator")
            # the launcher ends the input with 256; make it -1
            self.gen(("isz", ("-", ("var", s[2]), ("num", 256))), 1, 0)
            self.arith("*", self.const(257))
            self.fold(self.tmp(0))
            self.put(x)
            self.arith("-", self.tmp(0))
            self.fold(x)
        elif k == "putc":
            self.gen(s[2], self.bowl["__out"], 0)
        elif k == "out":
            for ch in s[2].encode("utf-8"):
                self.put(self.const(ch), self.bowl["__out"])
        elif k == "fail":
            msg = ("json2dir: " + s[2] + "\n").encode("utf-8")
            self.say("Clean the mixing bowl")
            for ch in reversed(msg):
                self.put(self.const(ch))
            self.serve(1)
        elif k == "halt":
            self.finish()
        elif k == "call":
            for x in self.procs[s[2]]:
                self.native(x, d)
        elif k == "if":
            self.native_if(s[2], s[3], d)
        elif k == "while":
            kind, e = norm(s[2])
            w = self.ctl(d, 0)
            self.cond_value(kind, e, w)
            v = self.open_loop(w)
            for x in s[3]:
                self.native(x, d + 1)
            self.cond_value(kind, e, w)
            self.close_loop(v)
        else:
            raise Error(f"line {s[1]}: {k} cannot be compiled here")

    def cond_value(self, kind, e, w):
        """w = something nonzero iff the condition holds."""
        if kind == "nz":
            self.gen(e, 1, 0)
        else:
            self.gen(("isz", e), 1, 0)
        self.fold(w)

    def native_if(self, arms, els, d):
        (c, body), rest = arms[0], arms[1:]
        if rest:
            els = [("if", 0, rest, els)]
        kind, e = norm(c)
        if kind == "z" and els:
            # if x == 0 {A} else {B}  is  if x != 0 {B} else {A}
            kind, body, els = "nz", els, body
        t, f = self.ctl(d, 0), self.ctl(d, 1)
        if not els:
            if kind == "z":
                e = ("isz", e)
            self.gen(e, 1, 0)
            self.fold(t)
            v = self.open_loop(t)
            for x in body:
                self.native(x, d + 1)
            if exact01(e):
                self.close_loop(v, t)
            else:
                self.set_to(t, self.const(0))
                self.close_loop(v)
            return
        self.gen(e, 1, 0)
        self.fold(t)
        self.set_to(f, self.const(1))
        v = self.open_loop(t)
        for x in body:
            self.native(x, d + 1)
        self.set_to(f, self.const(0))
        self.set_to(t, self.const(0))
        self.close_loop(v)
        v = self.open_loop(f)
        for x in els:
            self.native(x, d + 1)
        self.close_loop(v, f)

    def stack(self, name):
        if name not in self.stacks:
            raise Error(f"unknown stack {name!r}")
        return self.bowl[name]

    def serve(self, b):
        self.say(f"Liquefy contents of {self.bname(b)}")
        self.say(f"Pour contents of {self.bname(b)} into the baking dish")
        self.say("Refrigerate for 1 hour")

    def finish(self):
        """Reverse the output bowl (with -1 at the bottom) and serve it."""
        out, rev = self.bowl["__out"], self.bowl["__rev"]
        ch, t = self.n.ing(("sys", "ch")), self.tmp(0)
        self.fold(ch, out)
        self.put(ch)
        self.arith("+", self.const(1))
        self.fold(t)
        v = self.open_loop(t)
        self.put(ch, rev)
        self.fold(ch, out)
        self.put(ch)
        self.arith("+", self.const(1))
        self.fold(t)
        self.close_loop(v)
        self.serve(rev)

    # ------------------------------------------------ lowering to blocks

    def label(self):
        self.nlabels += 1
        return self.nlabels

    def lower(self, body, ctx, ir):
        """ctx: dict with 'brk', 'cont', 'ret' labels and 'proc' (real proc)."""
        for s in body:
            self.lower_stmt(s, ctx, ir)

    def lower_stmt(self, s, ctx, ir):
        k = s[0]
        if self.is_simple(s):
            ir.append(("native", s))
        elif k == "break":
            ir.append(("goto", ctx["brk"]))
        elif k == "continue":
            ir.append(("goto", ctx["cont"]))
        elif k == "return":
            if ctx["ret"] is None:
                ir.append(("ret", ctx["proc"]))
            else:
                ir.append(("goto", ctx["ret"]))
        elif k == "call":
            p = s[2]
            if p in self.recursive:
                back = self.label()
                ir.append(("call", p, back))
                ir.append(("label", back))
            else:
                end = self.label()
                self.lower(self.procs[p], dict(ctx, ret=end, brk=None, cont=None), ir)
                ir.append(("goto", end))
                ir.append(("label", end))
        elif k == "if":
            end = self.label()
            for c, b in s[2]:
                yes, no = self.label(), self.label()
                ir.append(("cgoto", c, yes, no))
                ir.append(("label", yes))
                self.lower(b, ctx, ir)
                ir.append(("goto", end))
                ir.append(("label", no))
            if s[3]:
                self.lower(s[3], ctx, ir)
            ir.append(("goto", end))
            ir.append(("label", end))
        elif k == "while":
            top, body, end = self.label(), self.label(), self.label()
            ir.append(("goto", top))
            ir.append(("label", top))
            if s[2] is not None:
                ir.append(("cgoto", s[2], body, end))
            else:
                ir.append(("goto", body))
            ir.append(("label", body))
            self.lower(s[3], dict(ctx, brk=end, cont=top), ir)
            ir.append(("goto", top))
            ir.append(("label", end))
        else:
            raise Error(f"line {s[1]}: cannot lower {k}")

    def build(self):
        ir = []
        self.entry = {}
        real = ["main"] + sorted(self.recursive - {"main"})
        for p in real:
            self.entry[p] = self.label()
            ir.append(("label", self.entry[p]))
            self.lower(self.procs[p], {"brk": None, "cont": None, "ret": None, "proc": p}, ir)
            if p == "main":
                ir.append(("native", ("halt", 0)))
                ir.append(("stop",))
            else:
                ir.append(("ret", p))
        # split into blocks: label -> [natives, terminator]
        blocks, order, cur = {}, [], None
        for ins in ir:
            if ins[0] == "label":
                if cur is not None and blocks[cur][1] is None:
                    blocks[cur][1] = ("goto", ins[1])
                cur = ins[1]
                blocks[cur] = [[], None]
                order.append(cur)
            elif cur is None or blocks[cur][1] is not None:
                continue            # unreachable
            elif ins[0] == "native":
                blocks[cur][0].append(ins[1])
            else:
                blocks[cur][1] = ins
        # call sites: return numbers per proc
        self.sites = {p: [] for p in real}
        for l in order:
            t = blocks[l][1]
            if t[0] == "call":
                self.sites[t[1]].append(t[2])
        # resolve empty goto blocks
        def resolve(l, seen=()):
            nat, t = blocks[l]
            if not nat and t[0] == "goto" and t[1] not in seen and l not in self.entry.values():
                return resolve(t[1], seen + (l,))
            return l
        alias = {l: resolve(l) for l in order}

        def targets(t):
            if t[0] == "goto":
                return [t[1]]
            if t[0] == "cgoto":
                return [t[2], t[3]]
            if t[0] == "call":
                return [self.entry[t[1]]]
            if t[0] == "ret":
                return list(self.sites[t[1]])
            return []

        def remap(t):
            if t[0] == "goto":
                return ("goto", alias[t[1]])
            if t[0] == "cgoto":
                a, b = alias[t[2]], alias[t[3]]
                return ("goto", a) if a == b else ("cgoto", t[1], a, b)
            if t[0] == "call":
                return ("call", t[1], alias[t[2]])
            return t
        for l in order:
            blocks[l][1] = remap(blocks[l][1])
        self.sites = {p: [alias[x] for x in v] for p, v in self.sites.items()}
        # merge a block into its only predecessor when that one just jumps to it
        changed = True
        while changed:
            changed = False
            live, todo = set(), [self.entry["main"]]
            while todo:
                l = todo.pop()
                if l in live:
                    continue
                live.add(l)
                todo.extend(targets(blocks[l][1]))
                if blocks[l][1][0] == "call":
                    todo.append(blocks[l][1][2])
            order = [l for l in order if l in live]
            preds = {l: 0 for l in order}
            for l in order:
                for x in targets(blocks[l][1]):
                    preds[x] += 1
                if blocks[l][1][0] == "call":
                    preds[blocks[l][1][2]] += 1
            for l in order:
                t = blocks[l][1]
                if t[0] == "goto" and t[1] != l and preds[t[1]] == 1 \
                        and t[1] not in self.entry.values():
                    m = t[1]
                    blocks[l][0].extend(blocks[m][0])
                    blocks[l][1] = blocks[m][1]
                    del blocks[m]
                    order.remove(m)
                    changed = True
                    break
        self.blocks, self.order = blocks, order

    # ------------------------------------------------ emitting

    def guard(self, l):
        return self.n.ing(("guard", l), 1 if l == self.entry["main"] else 0)

    def gguard(self, j):
        return self.n.ing(("group", j), 1 if j == self.group[self.entry["main"]] else 0)

    def jumps(self, here, assigns):
        """assigns: list of (label, cond) where cond is an ingredient holding a
        value that is nonzero iff control goes there (None: unconditionally)."""
        gl = {}
        for l, c in assigns:
            if c is None:
                self.set_to(self.guard(l), self.const(1))
            elif c != self.guard(l):
                self.set_to(self.guard(l), c)
            j = self.group[l]
            if j != self.group[here] or self.index[l] < self.index[here]:
                gl.setdefault(j, []).append(c)
            # forward in the same group: reached later in this pass; self: the block repeats
        for j, cs in gl.items():
            if None in cs or len(cs) > 1:
                if len(cs) > 1 and None not in cs and all(c is not None for c in cs):
                    # several possible targets in one group: one of them is taken
                    self.put(cs[0])
                    for c in cs[1:]:
                        self.arith("+", c)
                    self.fold(self.gguard(j))
                else:
                    self.set_to(self.gguard(j), self.const(1))
            else:
                self.set_to(self.gguard(j), cs[0])

    def emit_block(self, l):
        nat, t = self.blocks[l]
        g = self.guard(l)
        v = self.open_loop(g)
        self.set_to(g, self.const(0))
        for s in nat:
            self.native(s, 0)
        k = t[0]
        if k == "goto":
            self.jumps(l, [(t[1], None)])
        elif k == "cgoto":
            kind, e = norm(t[1])
            yes, no = (t[2], t[3]) if kind == "nz" else (t[3], t[2])
            # guard(yes) = e (nonzero iff e != 0), guard(no) = [e == 0]
            gy, gn = self.guard(yes), self.guard(no)
            self.gen(e, 1, 0)
            self.fold(gy)
            if exact01(e):
                self.put(gy)
                self.arith("*", self.const(-1))
                self.arith("+", self.const(1))
                self.fold(gn)
            else:
                tt = self.tmp(0)
                self.put(gy)
                self.fold(tt)
                self.set_to(gn, self.const(1))
                v2 = self.open_loop(tt)
                self.set_to(gn, self.const(0))
                self.set_to(tt, self.const(0))
                self.close_loop(v2)
            self.jumps(l, [(yes, gy), (no, gn)])
        elif k == "call":
            p, back = t[1], t[2]
            self.put(self.const(self.sites[p].index(back)), self.bowl["__call"])
            self.jumps(l, [(self.entry[p], None)])
        elif k == "ret":
            sites = self.sites[t[1]]
            if len(sites) == 1:
                self.fold(self.tmp(0), self.bowl["__call"])
                self.jumps(l, [(sites[0], None)])
            else:
                r = self.n.ing(("sys", "r"))
                self.fold(r, self.bowl["__call"])
                assigns = []
                for i, s in enumerate(sites):
                    self.gen_isz_r(r, i)
                    self.fold(self.guard(s))
                    assigns.append((s, self.guard(s)))
                self.jumps(l, assigns)
        elif k == "stop":
            pass
        else:
            raise Error(f"bad terminator {t!r}")
        self.close_loop(v)

    def gen_isz_r(self, r, i):
        self.put(r)
        if i:
            self.arith("-", self.const(i))
        t0, t1 = self.tmp(0), self.tmp(1)
        self.fold(t0)
        self.set_to(t1, self.const(1))
        v = self.open_loop(t0)
        self.set_to(t1, self.const(0))
        self.set_to(t0, self.const(0))
        self.close_loop(v)
        self.put(t1)

    def compile(self, group_size=12):
        self.build()
        self.index = {l: i for i, l in enumerate(self.order)}
        self.group = {l: i // group_size for i, l in enumerate(self.order)}
        ngroups = (len(self.order) + group_size - 1) // group_size
        # fix the order of the first ingredients: system things first
        body_start = len(self.lines)
        out = self.bowl["__out"]
        self.put(self.const(-1), out)          # the bottom of the output
        running = self.n.ing(("sys", "running"), 1)
        v = self.open_loop(running)
        for j in range(ngroups):
            gv = self.open_loop(self.gguard(j))
            self.set_to(self.gguard(j), self.const(0))
            for l in self.order[j * group_size:(j + 1) * group_size]:
                self.emit_block(l)
            self.close_loop(gv)
        self.close_loop(v)
        method = self.lines[body_start:]
        self.lines = []
        for name, k in self.inits:
            self.put(self.const(0))
            self.arith("-", self.const(k))
            self.fold(name)
        return self.lines + method


# ---------------------------------------------------------------- recipe text

MEASURES = ["g", "g", "g", "kg", "pinches", "ml", "dashes", "cups", "teaspoons",
            "tablespoons", "heaped tablespoons", "level teaspoons", "g", "ml"]
SINGULAR = {"pinches": "pinch", "dashes": "dash", "cups": "cup", "teaspoons": "teaspoon",
            "tablespoons": "tablespoon", "heaped tablespoons": "heaped tablespoon",
            "level teaspoons": "level teaspoon"}


def recipe(comp, method):
    out = []
    out.append("JSON-to-Directory Souffle.")
    out.append("")
    out.append("A light, airy souffle that rises from a JSON document into a whole tree of "
               "directories, files, symbolic links and executable scripts. The batter is the "
               "document itself, taken from the refrigerator one byte (as a decimal number) at "
               "a time; it is checked for UTF-8 and JSON lumps, every name is inspected, and "
               "only then is the shell script that builds the tree folded together, liquefied "
               "and poured into the baking dish. A spoiled batter yields only a note from the "
               "chef. Generated by gen/chef.py from json2dir.stk; Acme::Chef reads it.")
    out.append("")
    out.append("Ingredients.")
    for i, (key, name, value) in enumerate(comp.n.ingredients):
        meas = MEASURES[i % len(MEASURES)]
        if value == 1:
            meas = SINGULAR.get(meas, meas)
        out.append(f"{value} {meas} {name}")
    out.append("")
    out.append("Cooking time: 45 minutes.")
    out.append("")
    out.append("Pre-heat oven to 190 degrees Celsius gas mark 5.")
    out.append("")
    out.append("Method.")
    for depth, text in method:
        out.append("  " * depth + text)
    out.append("")
    out.append("Serves 1.")
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: chef.py program.stk > program.chef")
    with open(sys.argv[1], encoding="utf-8") as f:
        text = f.read()
    try:
        vars_, stacks, procs = Parser(tokenize(text)).program()
        comp = Compiler(vars_, stacks, procs)
        method = comp.compile()
    except Error as e:
        sys.exit(f"chef.py: {e}")
    sys.stdout.buffer.write(recipe(comp, method).encode("ascii"))


if __name__ == "__main__":
    main()
