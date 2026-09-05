"""Build the adapter from its existing canonical sources.

The source-checkout plugins already share these two stdlib catalog modules
with the host worker. Include them in the wheel and sdist without maintaining
another implementation or installing the worker into Hermes.
"""

from setuptools import setup
from setuptools.command.build_py import build_py


class AdapterBuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if package == "colony_hermes.colony_hostworker":
            modules.extend(
                (package, name, f"hostworker/colony_hostworker/{name}.py")
                for name in ("catalog", "contract")
            )
        return modules


setup(cmdclass={"build_py": AdapterBuildPy})
