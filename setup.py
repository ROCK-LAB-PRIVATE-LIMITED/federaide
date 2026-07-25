from setuptools import setup

try:
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
    class bdist_wheel(_bdist_wheel):
        def finalize_options(self):
            super().finalize_options()
            # Force setuptools to treat this as a non-pure wheel so it retains the platform tag
            self.root_is_pure = False
            
        def get_tag(self):
            # Force generic Python 3 tags ("py3", "none", <platform>)
            # This allows one wheel to install on Python 3.10, 3.11, 3.12, etc.
            python, abi, plat = super().get_tag()
            return ("py3", "none", plat)
except ImportError:
    bdist_wheel = None

cmdclass = {}
if bdist_wheel:
    cmdclass["bdist_wheel"] = bdist_wheel

setup(
    cmdclass=cmdclass,
)