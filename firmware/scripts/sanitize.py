# build_flags reach only the compiler; the sanitizer runtimes must be linked too.
# Import and env are SCons globals.
Import("env")  # noqa: F821

env.Append(LINKFLAGS=["-fsanitize=address,undefined", "-fno-sanitize-recover=all"])  # noqa: F821
