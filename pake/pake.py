import os
import re
import sys
import imp
import subprocess as sp
from collections import OrderedDict

enable_debug = False

## Errors

class PakeError(RuntimeError):
    pass

class InvalidCompilerError(PakeError):
    pass

class RuleError(PakeError):
    pass

class DuplicatedRuleError(RuleError):
    pass

## Compilers

class Compiler(object):
    pass

class GccLikeCompiler(Compiler):
    def translate(self, cli):
        return cli

    def as_command(self, cli):
        return [self.path] + cli.split(' ')

    def execute(self, command):
        p = sp.Popen(command, stdout=sys.stdout, stderr=sys.stderr)
        p.wait()
        return p.returncode

    def compile(self, cli):
        command = self.as_command(cli)
        say(' '.join(command))
        self.execute(command)
        return cli

    def link(self, cli):
        command = self.as_command(cli)
        say(' '.join(command))
        self.execute(command)
        return cli

class ClangCompiler(GccLikeCompiler):
    def __init__(self, path):
        output = sp.check_output([path, '--version'])
        if 'clang'.encode('ascii') not in output:
            raise InvalidCompilerError()
        ##
        self.path = path

class GccCompiler(GccLikeCompiler):
    def __init__(self, path):
        output = sp.check_output([path, '--version'])
        raise InvalidCompilerError()
        ##
        self.path = path

class MSVCCompiler(Compiler):
    def __init__(self, path):
        output = sp.check_output([path, '/V'])
        raise InvalidCompilerError()
        ##
        self.path = path

compiler_types = [
        ClangCompiler,
        GccCompiler,
        MSVCCompiler,
        ]

def find_compiler(c):
    def which(exe):
        if 'PATH' not in os.environ:
            raise PakeError('Unable to find PATH env variable')
        for path in os.environ['PATH'].split(':'):
            maybe_exe = os.path.join(path, exe)
            if os.path.exists(maybe_exe):
                return maybe_exe
        return None
    path = which(c)
    if path:
        for ct in compiler_types:
            try:
                return ct(path)
            except InvalidCompilerError:
                pass
    return None

## Lang

class Lang(object):
    pass

class CompiledLang(object):
    def __init__(self):
        self.compiler = None
        self.flags = ''
        self.lflags = ''

    def compile(self, cli, **kwargs):
        self.compiler.compile(format(cli, **kwargs))

    def link(self, cli, **kwargs):
        self.compiler.link(format(cli, **kwargs))

class C(CompiledLang):
    def __init__(self):
        self.compiler = find_compiler('clang')

class Cxx(CompiledLang):
    def __init__(self):
        self.compiler = find_compiler('clang++')

c = C()
cxx = Cxx()

## Rules

rules = OrderedDict()
rules_pattern = []

def deps_as_list(deps):
    if isinstance(deps, list):
        return deps
    else:
        return [deps]

def get_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None

class Rule(object):
    def __init__(self, r, deps=[], name=None):
        self.rule = r
        self.deps = deps
        self.name = name if name else r.__name__
        ##
        if self.name in rules:
            raise DuplicatedRuleError('Found rule "{}" twice!'.format(self.name))
        dbg('Rule: name="{}", deps={}'.format(self.name, self.deps))
        rules[self.name] = self

    def __call__(self):
        outdated = False
        deps = deps_as_list(self.deps)
        ## Get current modification time
        mtime = get_mtime(self.name)
        ## First, lookup for all deps and check if they are files
        for dep in deps:
            ## Walk down rules to check if they are up-to-date
            if dep in rules:
                rules[dep]()
            ## At this point, the dep can either be a plain file/dir or a rule
            ## that must be instanciated using pattern rules
            ## 1. Try to create a rule
            else:
                dbg('No rule for: {}, trying to make one:'.format(dep))
                ## Instanciate rule with any rules_pattern found
                for r in rules_pattern:
                    if r.match(dep):
                        dbg('- {}: matched! Creating rule using it'.format(r.name))
                        ## Creates it and calls it
                        r = r.as_rule(dep)
                        r()
                        break
                    else:
                        dbg('- {}: does not matched.'.format(r.name))
            ## 2. At this point, a rule has been created for creating the dep, although,
            ##    the rule might generate a file (with the same name as the rule) ot not.
            ##    If it's a file, check for modifitication time
            if os.path.exists(dep):
                ## Now check for mtime
                dep_mtime = get_mtime(dep)
                ## IF outdated, then call the rule
                if mtime is None:
                    outdated = True
                elif dep_mtime is None:
                    raise RuleError(
                            'Unable to get modification time for dependency: "{}"'.format(dep))
                elif dep_mtime > mtime:
                    outdated = True
            ## 3. Now we know that there is something wrong cause we were not able to "create"
            ##    the dependency (cause none pattern-rules matched the given dep and because
            ##    no file matches the dep too). Stop it now!
            elif dep not in rules:
                raise RuleError('no matching rule for "{}"'.format(dep))
            ## 4. Consider the dep outdated if none file has been generated with the rule
            else:
                outdated = True
        ## Call the rule if outdated
        if outdated:
            self.rule(self)

    @property
    def input(self):
        return ' '.join(deps_as_list(self.deps))

    @property
    def output(self):
        return self.name

class RulePattern(object):
    def __init__(self, r, pattern, deps=[], name=None):
        self.rule = r
        self.deps = deps
        self.name = name if name else r.__name__
        ##
        self.pattern = pattern
        self.r_pattern = re.compile('^{}$'.format(self._replace_any_by(pattern, '(.*)')))
        ##
        dbg('RulePattern: name="{}", pattern="{}", deps={}'.format(self.name, self.pattern, self.deps))
        rules_pattern.append(self)

    def _replace_any_by(self, pattern, by):
        r = ''
        l = len(pattern)
        for i in range(l):
            if pattern[i:i+1] == '%%':
                r += '%'
            elif pattern[i] == '%':
                r += by
            else:
                r += pattern[i]
        return r

    def match(self, x):
        return self.r_pattern.match(x)

    def as_rule(self, x):
        m = self.match(x)
        if not m:
            raise RuleError('Unable to rulify "{}" using rule-pattern "{}"'.format(x, self.name))
        ## Get matched pattern
        x = m.group(1)
        name = self._replace_any_by(self.pattern, x)
        deps = []
        for dep in deps_as_list(self.deps):
            deps.append(self._replace_any_by(dep, x))
        return Rule(self.rule, deps=deps, name=name)

def rule(deps=[], pattern=False, name=None):
    def _rule(r):
        if pattern:
            return RulePattern(r, pattern=pattern, deps=deps, name=name)
        else:
            return Rule(r, deps=deps, name=name)
    return _rule

## Misc

def format(fmt, **kwargs):
    return fmt.format(c=c, cxx=cxx, **kwargs)

def dbg(what):
    if enable_debug:
        sys.stderr.write(':: dbg: ')
        sys.stderr.write(str(what))
        sys.stderr.write('\n')

def say(what):
    sys.stdout.write('\r')
    sys.stdout.write(':: ')
    sys.stdout.write(str(what))
    sys.stdout.write('\n')

def warn(what):
    sys.stderr.write('** ')
    sys.stderr.write('warning: ')
    sys.stderr.write(str(what))
    sys.stderr.write('\n')

def error(what):
    sys.stderr.write('** ')
    sys.stderr.write('error: ')
    sys.stderr.write(str(what))
    sys.stderr.write('\n')

def pake(path, target):
    if not os.path.exists(path):
        raise PakeError('no such file: {}'.format(path))
    p = imp.load_source('Pakefile', path)
    if target is None:
        target = p.rules.keys()[0]
    if target in p.rules:
        p.rules[target]()
    else:
        raise RuleError('target "{}" not found'.format(target))
