import calendar
from contextlib import contextmanager
import ctypes
from ctypes.util import find_library
import imp
from importlib import import_module, metadata, reload, resources
import importlib.util
from importlib.util import cache_from_source, MAGIC_NUMBER
import marshal
import os
from os.path import dirname, exists, join, realpath, relpath, splitext
import pkgutil
import platform
import re
from shutil import rmtree
import sys
from traceback import format_exc
import types
from warnings import catch_warnings, filterwarnings

from java.android import importer

from ..test_utils import API_LEVEL, FilterWarningsCase
from . import ABI, context


REQUIREMENTS = ["chaquopy-libcxx", "murmurhash", "Pygments", "extract-packages"]

# REQS_COMMON_ZIP and REQS_ABI_ZIP are now both extracted into the same directory, but we
# maintain the distinction in the tests in case that changes again in the future.
APP_ZIP = "app"
REQS_COMMON_ZIP = "requirements-common"
multi_abi = len([name for name in context.getAssets().list("chaquopy")
                 if name.startswith("requirements")]) > 2
REQS_ABI_ZIP = f"requirements-{ABI}" if multi_abi else REQS_COMMON_ZIP

def asset_path(zip_name, *paths):
    return join(realpath(context.getFilesDir().toString()), "chaquopy/AssetFinder",
                zip_name.partition("-")[0], *paths)


class TestAndroidImport(FilterWarningsCase):

    def test_bootstrap(self):
        chaquopy_dir = join(str(context.getFilesDir()), "chaquopy")
        self.assertCountEqual(["AssetFinder", "bootstrap-native", "bootstrap.imy",
                               "cacert.pem", "stdlib-common.imy"],
                              os.listdir(chaquopy_dir))
        bn_dir = f"{chaquopy_dir}/bootstrap-native"
        self.assertCountEqual([ABI], os.listdir(bn_dir))

        for subdir, entries in [
            # PythonPlugin.groovy explains why each of these modules are needed.
            (ABI, ["java", "_ctypes.so", "_datetime.so", "_random.so", "_sha512.so",
                   "_struct.so", "binascii.so",  "math.so", "mmap.so", "zlib.so"]),
            (f"{ABI}/java", ["chaquopy.so"]),
        ]:
            with self.subTest(subdir=subdir):
                # Create a stray file which should be removed on the next startup.
                pid_txt = f"{os.getpid()}.txt"
                with open(f"{bn_dir}/{subdir}/{pid_txt}", "w"):
                    pass
                self.assertCountEqual(entries + [pid_txt], os.listdir(f"{bn_dir}/{subdir}"))

                # If any of the bootstrap modules haven't been imported, that means they
                # no longer need to be in the bootstrap.
                if subdir == ABI:
                    for filename in entries:
                        with self.subTest(filename=filename):
                            self.assertIn(filename.replace(".so", ""), sys.modules)

    def test_init(self):
        self.check_py("murmurhash", REQS_COMMON_ZIP, "murmurhash/__init__.py", "get_include",
                      is_package=True)
        self.check_py("android1", APP_ZIP, "android1/__init__.py", "x",
                      source_head="# This package is used by TestAndroidImport.", is_package=True)

    def test_py(self):
        self.check_py("murmurhash.about", REQS_COMMON_ZIP, "murmurhash/about.py", "__summary__")
        self.check_py("android1.mod1", APP_ZIP, "android1/mod1.py",
                      "x", source_head='x = "android1.mod1"')

    def check_py(self, mod_name, zip_name, zip_path, existing_attr, **kwargs):
        filename = asset_path(zip_name, zip_path)
        # build.gradle has pyc { src false }, so APP_ZIP will generate __pycache__ directories.
        cache_filename = cache_from_source(filename) if zip_name == APP_ZIP else None
        mod = self.check_module(mod_name, filename, cache_filename, **kwargs)
        self.assertNotPredicate(exists, filename)
        if cache_filename is None:
            self.assertNotPredicate(exists, cache_from_source(filename))

        new_attr = "check_py_attr"
        self.assertFalse(hasattr(mod, new_attr))
        setattr(mod, new_attr, 1)
        delattr(mod, existing_attr)
        reload(mod)  # Should reuse existing module object.
        self.assertEqual(1, getattr(mod, new_attr))
        self.assertTrue(hasattr(mod, existing_attr))

        if cache_filename:
            # A valid .pyc should not be written again.
            with self.assertNotModifies(cache_filename):
                mod = self.clean_reload(mod)
            self.assertFalse(hasattr(mod, new_attr))

            # And if the header matches, the code in the .pyc should be used, whatever it is.
            header = self.read_pyc_header(cache_filename)
            with open(cache_filename, "wb") as pyc_file:
                pyc_file.write(header)
                code = compile(f"{new_attr} = 2", "<test>", "exec")
                marshal.dump(code, pyc_file)
            mod = self.clean_reload(mod)
            self.assertEqual(2, getattr(mod, new_attr))
            self.assertFalse(hasattr(mod, existing_attr))

            # A .pyc with mismatching header timestamp should be written again.
            new_header = header[0:8] + b"\x00\x01\x02\x03" + header[12:]
            self.assertNotEqual(new_header, header)
            self.write_pyc_header(cache_filename, new_header)
            with self.assertModifies(cache_filename):
                self.clean_reload(mod)
            self.assertEqual(header, self.read_pyc_header(cache_filename))

    def read_pyc_header(self, filename):
        with open(filename, "rb") as pyc_file:
            return pyc_file.read(16)

    def write_pyc_header(self, filename, header):
        with open(filename, "r+b") as pyc_file:
            pyc_file.seek(0)
            pyc_file.write(header)

    def test_so(self):
        filename = asset_path(REQS_ABI_ZIP, "murmurhash/mrmr.so")
        mod = self.check_module("murmurhash.mrmr", filename, filename)
        self.check_extract_if_changed(mod, filename)

    def test_ctypes(self):
        def assertHasSymbol(dll, name):
            self.assertIsNotNone(getattr(dll, name))
        def assertNotHasSymbol(dll, name):
            with self.assertRaises(AttributeError):
                getattr(dll, name)

        # Library extraction caused by CDLL.
        from murmurhash import mrmr
        os.remove(mrmr.__file__)
        ctypes.CDLL(mrmr.__file__)
        self.assertPredicate(exists, mrmr.__file__)

        # Library extraction caused by find_library.
        LIBCXX_FILENAME = asset_path(REQS_ABI_ZIP, "chaquopy/lib/libc++_shared.so")
        os.remove(LIBCXX_FILENAME)
        find_library_result = find_library("c++_shared")
        self.assertIsInstance(find_library_result, str)

        # This test covers non-Python libraries: for Python modules, see pyzmq/test.py.
        if (platform.architecture()[0] == "64bit") and (API_LEVEL < 23):
            self.assertNotIn("/", find_library_result)
        else:
            self.assertEqual(LIBCXX_FILENAME, find_library_result)

        # Whether find_library returned an absolute filename or not, the file should have been
        # extracted and the return value should be accepted by CDLL.
        self.assertPredicate(exists, LIBCXX_FILENAME)
        libcxx = ctypes.CDLL(find_library_result)
        assertHasSymbol(libcxx, "_ZSt9terminatev")  # std::terminate()
        assertNotHasSymbol(libcxx, "nonexistent")

        # System libraries.
        self.assertIsNotNone(find_library("c"))
        self.assertIsNotNone(find_library("log"))
        self.assertIsNone(find_library("nonexistent"))

        libc = ctypes.CDLL(find_library("c"))
        liblog = ctypes.CDLL(find_library("log"))
        assertHasSymbol(libc, "printf")
        assertHasSymbol(liblog, "__android_log_write")
        assertNotHasSymbol(libc, "__android_log_write")

        # Global search (https://bugs.python.org/issue34592)
        main = ctypes.CDLL(None)
        assertHasSymbol(main, "printf")
        assertHasSymbol(main, "__android_log_write")
        assertNotHasSymbol(main, "nonexistent")

        assertHasSymbol(ctypes.pythonapi, "PyObject_Str")

    def test_non_package_data(self):
        for dir_name, dir_description in [("", "root"), ("non_package_data", "directory"),
                                          ("non_package_data/subdir", "subdirectory")]:
            with self.subTest(dir_name=dir_name):
                extracted_dir = asset_path(APP_ZIP, dir_name)
                self.assertCountEqual(
                    ["non_package_data.txt"] + (["test.pth"] if not dir_name else []),
                    [entry.name for entry in os.scandir(extracted_dir) if entry.is_file()])
                with open(join(extracted_dir, "non_package_data.txt")) as f:
                    self.assertPredicate(str.startswith, f.read(),
                                         f"# Text file in {dir_description}")

        # Package directories shouldn't be extracted on startup, but on first import. This
        # package is never imported, so it should never be extracted at all.
        self.assertNotPredicate(exists, asset_path(APP_ZIP, "never_imported"))

    def test_package_data(self):
        # App ZIP
        pkg = "android1"
        self.check_data(APP_ZIP, pkg, "__init__.py", b"# This package is")
        self.check_data(APP_ZIP, pkg, "b.so", b"bravo")
        self.check_data(APP_ZIP, pkg, "a.txt", b"alpha")
        self.check_data(APP_ZIP, pkg, "subdir/c.txt", b"charlie")

        # Requirements ZIP
        self.reset_package("murmurhash")
        self.check_data(REQS_COMMON_ZIP, "murmurhash", "about.pyc", MAGIC_NUMBER)
        self.check_data(REQS_ABI_ZIP, "murmurhash", "mrmr.so", b"\x7fELF")
        self.check_data(REQS_COMMON_ZIP, "murmurhash", "mrmr.pxd", b"from libc.stdint")

        import murmurhash.about
        loader = murmurhash.about.__loader__
        zip_name = REQS_COMMON_ZIP
        with self.assertRaisesRegex(ValueError,
                                    r"AssetFinder\('{}'\) can't access '/invalid.py'"
                                    .format(asset_path(zip_name, "murmurhash"))):
            loader.get_data("/invalid.py")
        with self.assertRaisesRegex(FileNotFoundError, "invalid.py"):
            loader.get_data(asset_path(zip_name, "invalid.py"))

    def check_data(self, zip_name, package, filename, start):
        # Extraction is triggered only when a top-level package is imported.
        self.assertNotIn(".", package)

        cache_filename = asset_path(zip_name, package, filename)
        mod = import_module(package)
        data = pkgutil.get_data(package, filename)
        self.assertTrue(data.startswith(start))

        if splitext(filename)[1] in [".py", ".pyc", ".so"]:
            # Importable files are not extracted.
            self.assertNotPredicate(exists, cache_filename)
        else:
            self.check_extract_if_changed(mod, cache_filename)
            with open(cache_filename, "rb") as cache_file:
                self.assertEqual(data, cache_file.read())

    def check_extract_if_changed(self, mod, cache_filename):
        # A missing file should be extracted.
        if exists(cache_filename):
            os.remove(cache_filename)
        mod = self.clean_reload(mod)
        self.assertPredicate(exists, cache_filename)

        # An unchanged file should not be extracted again.
        with self.assertNotModifies(cache_filename):
            mod = self.clean_reload(mod)

        # A file with mismatching mtime should be extracted again.
        original_mtime = os.stat(cache_filename).st_mtime
        os.utime(cache_filename, None)
        with self.assertModifies(cache_filename):
            self.clean_reload(mod)
        self.assertEqual(original_mtime, os.stat(cache_filename).st_mtime)

    def test_extract_packages(self):
        self.check_extract_packages("ep_alpha", [])
        self.check_extract_packages("ep_bravo", [
            "__init__.py", "mod.py", "one/__init__.py", "two/__init__.py"
        ])
        self.check_extract_packages("ep_charlie", ["one/__init__.py"])

        # If a module has both a .py and a .pyc file, the .pyc file should be used because
        # it'll load faster.
        import ep_bravo
        py_path = asset_path(REQS_COMMON_ZIP, "ep_bravo/__init__.py")
        self.assertEqual(py_path, ep_bravo.__file__)
        self.assertEqual(py_path + "c", ep_bravo.__spec__.origin)

    def check_extract_packages(self, package, files):
        mod = import_module(package)
        cache_dir = asset_path(REQS_COMMON_ZIP, package)
        self.assertEqual(cache_dir, dirname(mod.__file__))
        if exists(cache_dir):
            rmtree(cache_dir)

        self.clean_reload(mod)
        if not files:
            self.assertNotPredicate(exists, cache_dir)
        else:
            self.assertCountEqual(files,
                                 [relpath(join(dirpath, name), cache_dir)
                                  for dirpath, _, filenames in os.walk(cache_dir)
                                  for name in filenames])
            for path in files:
                with open(f"{cache_dir}/{path}") as file:
                    self.assertEqual(f"# This file is {package}/{path}\n", file.read())

    def clean_reload(self, mod):
        sys.modules.pop(mod.__name__, None)
        submod_names = [name for name in sys.modules if name.startswith(mod.__name__ + ".")]
        for name in submod_names:
            sys.modules.pop(name)

        # For extension modules, this may reuse the same module object (see create_dynamic
        # in import.c).
        return import_module(mod.__name__)

    def check_module(self, mod_name, filename, cache_filename, *, is_package=False,
                     source_head=None):
        if cache_filename and exists(cache_filename):
            os.remove(cache_filename)
        mod = import_module(mod_name)
        mod = self.clean_reload(mod)
        if cache_filename:
            self.assertPredicate(exists, cache_filename)

        # Module attributes
        self.assertEqual(mod_name, mod.__name__)
        self.assertEqual(filename, mod.__file__)
        self.assertEqual(realpath(mod.__file__), mod.__file__)
        self.assertEqual(filename.endswith(".so"), exists(mod.__file__))
        if is_package:
            self.assertEqual([dirname(filename)], mod.__path__)
            self.assertEqual(realpath(mod.__path__[0]), mod.__path__[0])
            self.assertEqual(mod_name, mod.__package__)
        else:
            self.assertFalse(hasattr(mod, "__path__"))
            self.assertEqual(mod_name.rpartition(".")[0], mod.__package__)
        loader = mod.__loader__
        self.assertIsInstance(loader, importer.AssetLoader)

        # When importlib._bootstrap._init_module_attrs is passed an already-initialized
        # module with override=False, it sets __spec__ and leaves the other attributes
        # alone. So if the module object was reused in clean_reload, then __loader__ and
        # __spec__.loader may be equal but not identical.
        spec = mod.__spec__
        self.assertEqual(mod_name, spec.name)
        self.assertEqual(loader, spec.loader)
        # spec.origin and the extract_so symlink workaround are covered by pyzmq/test.py.

        # Loader methods (get_data is tested elsewhere)
        self.assertEqual(is_package, loader.is_package(mod_name))
        self.assertIsInstance(loader.get_code(mod_name),
                              types.CodeType if filename.endswith(".py") else type(None))

        source = loader.get_source(mod_name)
        if source_head:
            self.assertTrue(source.startswith(source_head), repr(source))
        else:
            self.assertIsNone(source)

        self.assertEqual(re.sub(r"\.pyc$", ".py", loader.get_filename(mod_name)),
                         mod.__file__)

        return mod

    # Verify that the traceback builder can get source code from the loader in all contexts.
    # (The "package1" test files are also used in TestImport.)
    def test_exception(self):
        col_marker = r'( +\^+\n)?'  # Column marker (Python >= 3.11)
        test_frame = (
            fr'  File "{asset_path(APP_ZIP)}/chaquopy/test/android/test_import.py", '
            fr'line \d+, in test_exception\n'
            fr'    .+?\n'  # Source code line from this file
            + col_marker)
        import_frame = r'  File "import.pxi", line \d+, in java.chaquopy.import_override\n'

        # Compilation
        try:
            from package1 import syntax_error  # noqa
        except SyntaxError:
            self.assertRegex(
                format_exc(),
                test_frame + import_frame +
                fr'  File "{asset_path(APP_ZIP)}/package1/syntax_error.py", line 1\n'
                fr'    one two\n'
                fr'        \^(\^\^)?\n'
                fr'SyntaxError: invalid syntax\n$')
        else:
            self.fail()

        # Module execution
        try:
            from package1 import recursive_import_error  # noqa
        except ImportError:
            self.assertRegex(
                format_exc(),
                test_frame + import_frame +
                fr'  File "{asset_path(APP_ZIP)}/package1/recursive_import_error.py", '
                fr'line 1, in <module>\n'
                fr'    from os import nonexistent\n'
                fr"ImportError: cannot import name 'nonexistent' from 'os'")
        else:
            self.fail()

        # Module execution (recursive import)
        try:
            from package1 import recursive_other_error  # noqa
        except ValueError:
            self.assertRegex(
                format_exc(),
                test_frame + import_frame +
                fr'  File "{asset_path(APP_ZIP)}/package1/recursive_other_error.py", '
                fr'line 1, in <module>\n'
                fr'    from . import other_error  # noqa: F401\n' +
                col_marker +
                import_frame +
                fr'  File "{asset_path(APP_ZIP)}/package1/other_error.py", '
                fr'line 1, in <module>\n'
                fr'    int\("hello"\)\n'
                fr"ValueError: invalid literal for int\(\) with base 10: 'hello'\n$")
        else:
            self.fail()

        # After import complete.
        # Frames from pre-compiled requirements should have no source code.
        try:
            import murmurhash
            murmurhash_file = murmurhash.__file__
            del murmurhash.__file__
            murmurhash.get_include()
        except NameError:
            self.assertRegex(
                format_exc(),
                test_frame +
                fr'  File "{asset_path(REQS_COMMON_ZIP)}/murmurhash/__init__.py", '
                fr'line 5, in get_include\n'
                fr"NameError: name '__file__' is not defined\n$")
        else:
            self.fail()
        finally:
            murmurhash.__file__ = murmurhash_file

        # Frames from pre-compiled stdlib should have filenames starting with "stdlib/", and no
        # source code.
        try:
            import json
            json.loads("hello")
        except json.JSONDecodeError:
            self.assertRegex(
                format_exc(),
                test_frame +
                r'  File "stdlib/json/__init__.py", line \d+, in loads\n'
                r'  File "stdlib/json/decoder.py", line \d+, in decode\n'
                r'  File "stdlib/json/decoder.py", line \d+, in raw_decode\n'
                r'json.decoder.JSONDecodeError: Expecting value: line 1 column 1 \(char 0\)\n$')
        else:
            self.fail()

    def test_imp(self):
        with catch_warnings():
            filterwarnings("default", category=DeprecationWarning)

            with self.assertRaisesRegex(ImportError, "No module named 'nonexistent'"):
                imp.find_module("nonexistent")

            # See comment about torchvision below.
            from murmurhash import mrmr
            os.remove(mrmr.__file__)

            # If any of the below modules already exist, they will be reloaded. This may have
            # side-effects, e.g. if we'd included sys, then sys.executable would be reset and
            # test_sys below would fail.
            for mod_name, expected_type in [
                    ("dbm", imp.PKG_DIRECTORY),                     # stdlib
                    ("argparse", imp.PY_COMPILED),                  #
                    ("select", imp.C_EXTENSION),                    #
                    ("errno", imp.C_BUILTIN),                       #
                    ("murmurhash", imp.PKG_DIRECTORY),              # requirements
                    ("murmurhash.about", imp.PY_COMPILED),          #
                    ("murmurhash.mrmr", imp.C_EXTENSION),           #
                    ("chaquopy.utils", imp.PKG_DIRECTORY),          # app (already loaded)
                    ("imp_test", imp.PY_SOURCE)]:                   #     (not already loaded)
                with self.subTest(mod_name=mod_name):
                    path = None
                    prefix = ""
                    words = mod_name.split(".")
                    for i, word in enumerate(words):
                        prefix += word
                        with self.subTest(prefix=prefix):
                            file, pathname, description = imp.find_module(word, path)
                            suffix, mode, actual_type = description

                            if actual_type in [imp.C_BUILTIN, imp.PKG_DIRECTORY]:
                                self.assertIsNone(file)
                                self.assertEqual("", suffix)
                                self.assertEqual("", mode)
                            else:
                                data = file.read()
                                self.assertEqual(0, len(data))
                                if actual_type == imp.PY_SOURCE:
                                    self.assertEqual("r", mode)
                                    self.assertIsInstance(data, str)
                                else:
                                    self.assertEqual("rb", mode)
                                    self.assertIsInstance(data, bytes)
                                self.assertPredicate(str.endswith, pathname, suffix)

                            # See comment about torchvision in find_module_override.
                            if actual_type == imp.C_EXTENSION:
                                self.assertPredicate(exists, pathname)

                            mod = imp.load_module(prefix, file, pathname, description)
                            self.assertEqual(prefix, mod.__name__)
                            self.assertEqual(actual_type == imp.PKG_DIRECTORY,
                                             hasattr(mod, "__path__"))
                            self.assertIsNotNone(mod.__spec__)
                            self.assertEqual(mod.__name__, mod.__spec__.name)

                            if actual_type == imp.C_BUILTIN:
                                self.assertIsNone(pathname)
                            elif actual_type == imp.PKG_DIRECTORY:
                                self.assertEqual(pathname, dirname(mod.__file__))
                            else:
                                self.assertEqual(re.sub(r"\.pyc$", ".py", pathname),
                                                 re.sub(r"\.pyc$", ".py", mod.__file__))

                            if i < len(words) - 1:
                                self.assertEqual(imp.PKG_DIRECTORY, actual_type)
                                prefix += "."
                                path = mod.__path__
                            else:
                                self.assertEqual(expected_type, actual_type)

    # This trick was used by Electron Cash to load modules under a different name. The Electron
    # Cash Android app no longer needs it, but there may be other software which does.
    def test_imp_rename(self):
        with catch_warnings():
            filterwarnings("default", category=DeprecationWarning)

            # Clean start to allow test to be run more than once.
            for name in list(sys.modules):
                if name.startswith("imp_rename"):
                    del sys.modules[name]

            # Renames in stdlib are not currently supported.
            with self.assertRaisesRegex(ImportError, "ChaquopyZipImporter does not support "
                                        "loading module 'json' under a different name 'jason'"):
                imp.load_module("jason", *imp.find_module("json"))

            def check_top_level(real_name, load_name, id):
                mod_renamed = imp.load_module(load_name, *imp.find_module(real_name))
                self.assertEqual(load_name, mod_renamed.__name__)
                self.assertEqual(id, mod_renamed.ID)
                self.assertIs(mod_renamed, import_module(load_name))

                mod_original = import_module(real_name)
                self.assertEqual(real_name, mod_original.__name__)
                self.assertIsNot(mod_renamed, mod_original)
                self.assertEqual(mod_renamed.ID, mod_original.ID)
                self.assertEqual(mod_renamed.__file__, mod_original.__file__)

            check_top_level("imp_rename_one", "imp_rename_1", "1")  # Module
            check_top_level("imp_rename_two", "imp_rename_2", "2")  # Package

            import imp_rename_two  # Original
            import imp_rename_2    # Renamed
            path = [asset_path(APP_ZIP, "imp_rename_two")]
            self.assertEqual(path, imp_rename_two.__path__)
            self.assertEqual(path, imp_rename_2.__path__)

            # Non-renamed sub-modules
            from imp_rename_2 import mod_one, pkg_two
            for mod, name, id in [(mod_one, "mod_one", "21"), (pkg_two, "pkg_two", "22")]:
                self.assertFalse(hasattr(imp_rename_two, name), name)
                mod_attr = getattr(imp_rename_2, name)
                self.assertIs(mod_attr, mod)
                self.assertEqual("imp_rename_2." + name, mod.__name__)
                self.assertEqual(id, mod.ID)
            self.assertEqual([asset_path(APP_ZIP, "imp_rename_two/pkg_two")], pkg_two.__path__)

            # Renamed sub-modules
            mod_3 = imp.load_module("imp_rename_2.mod_3",
                                    *imp.find_module("mod_three", imp_rename_two.__path__))
            self.assertEqual("imp_rename_2.mod_3", mod_3.__name__)
            self.assertEqual("23", mod_3.ID)
            self.assertIs(sys.modules["imp_rename_2.mod_3"], mod_3)

        # The standard load_module implementation doesn't add a sub-module as an attribute of
        # its package. Despite this, it can still be imported under its new name using `from
        # ... import`. This seems to contradict the documentation of __import__, but it's not
        # important enough to investigate just now.
        self.assertFalse(hasattr(imp_rename_2, "mod_3"))

    # Ensure that a package can be imported by a bare name when its in an AssetFinder subdirectory.
    # This is typically done when vendoring packages and dynamically adjusting sys.path. See #820.
    def test_imp_subdir(self):
        sys.modules.pop("about", None)
        murmur_path = asset_path(REQS_COMMON_ZIP, "murmurhash")
        about_path = join(murmur_path, "about.py")
        sys.path.insert(0, murmur_path)
        try:
            import about
        finally:
            sys.path.remove(murmur_path)
        self.assertEqual(about_path, about.__file__)

    # Make sure the standard library importer implements the new loader API
    # (https://stackoverflow.com/questions/63574951).
    def test_zipimport(self):
        for mod_name in ["zipfile",  # Imported during bootstrap
                         "wave"]:    # Imported after bootstrap
            with self.subTest(mod_name=mod_name):
                old_mod = import_module(mod_name)
                spec = importlib.util.find_spec(mod_name)
                new_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(new_mod)

                self.assertIsNot(new_mod, old_mod)
                for attr_name in ["__name__", "__file__"]:
                    with self.subTest(attr_name=attr_name):
                        self.assertEqual(getattr(new_mod, attr_name),
                                         getattr(old_mod, attr_name))

    # See src/test/python/test.pth.
    def test_pth(self):
        import pth_generated
        self.assertFalse(hasattr(pth_generated, "__file__"))
        self.assertEqual([asset_path(APP_ZIP, "pth_generated")], pth_generated.__path__)
        for entry in sys.path:
            self.assertNotIn("nonexistent", entry)

    def test_iter_modules(self):
        def check_iter_modules(mod, expected):
            mod_infos = list(pkgutil.iter_modules(mod.__path__))
            self.assertCountEqual(expected, [(mi.name, mi.ispkg) for mi in mod_infos])
            finders = [pkgutil.get_importer(p) for p in mod.__path__]
            for mi in mod_infos:
                self.assertIn(mi.module_finder, finders, mi)

        import murmurhash.tests
        check_iter_modules(murmurhash, [("about", False),   # Pure-Python module
                                        ("mrmr", False),    # Native module
                                        ("tests", True)])   # Package
        check_iter_modules(murmurhash.tests, [("test_import", False)])

        self.assertCountEqual([("murmurhash.about", False), ("murmurhash.mrmr", False),
                               ("murmurhash.tests", True),
                               ("murmurhash.tests.test_import", False)],
                              [(mi.name, mi.ispkg) for mi in
                               pkgutil.walk_packages(murmurhash.__path__, "murmurhash.")])

    def test_pr_distributions(self):
        import pkg_resources as pr
        self.assertCountEqual(REQUIREMENTS, [dist.project_name for dist in pr.working_set])
        self.assertEqual("0.28.0", pr.get_distribution("murmurhash").version)

    def test_pr_resources(self):
        import pkg_resources as pr

        # App ZIP
        pkg = "android1"
        names = ["subdir", "__init__.py", "a.txt", "b.so", "mod1.py"]
        self.assertCountEqual(names, pr.resource_listdir(pkg, ""))
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(pr.resource_exists(pkg, name))
                self.assertEqual(pr.resource_isdir(pkg, name),
                                 name == "subdir")
        self.assertFalse(pr.resource_exists(pkg, "nonexistent"))
        self.assertFalse(pr.resource_isdir(pkg, "nonexistent"))

        self.assertCountEqual(["c.txt"], pr.resource_listdir(pkg, "subdir"))
        self.assertTrue(pr.resource_exists(pkg, "subdir/c.txt"))
        self.assertFalse(pr.resource_isdir(pkg, "subdir/c.txt"))
        self.assertFalse(pr.resource_exists(pkg, "subdir/nonexistent.txt"))

        self.check_pr_resource(APP_ZIP, pkg, "__init__.py", b"# This package is")
        self.check_pr_resource(APP_ZIP, pkg, "a.txt", b"alpha\n")
        self.check_pr_resource(APP_ZIP, pkg, "b.so", b"bravo\n")
        self.check_pr_resource(APP_ZIP, pkg, "subdir/c.txt", b"charlie\n")

        # Requirements ZIP
        self.reset_package("murmurhash")
        self.assertCountEqual(["include", "tests", "__init__.pxd", "__init__.pyc", "about.pyc",
                               "mrmr.pxd", "mrmr.pyx", "mrmr.so"],
                              pr.resource_listdir("murmurhash", ""))
        self.assertCountEqual(["MurmurHash2.h", "MurmurHash3.h"],
                              pr.resource_listdir("murmurhash", "include/murmurhash"))

        self.check_pr_resource(REQS_COMMON_ZIP, "murmurhash", "__init__.pyc", MAGIC_NUMBER)
        self.check_pr_resource(REQS_COMMON_ZIP, "murmurhash", "mrmr.pxd", b"from libc.stdint")
        self.check_pr_resource(REQS_ABI_ZIP, "murmurhash", "mrmr.so", b"\x7fELF")

    def check_pr_resource(self, zip_name, package, filename, start):
        import pkg_resources as pr
        with self.subTest(package=package, filename=filename):
            data = pr.resource_string(package, filename)
            self.assertPredicate(data.startswith, start)

            abs_filename = pr.resource_filename(package, filename)
            self.assertEqual(asset_path(zip_name, package.replace(".", "/"), filename),
                             abs_filename)
            if splitext(filename)[1] in [".py", ".pyc", ".so"]:
                # Importable files are not extracted.
                self.assertNotPredicate(exists, abs_filename)
            else:
                with open(abs_filename, "rb") as f:
                    self.assertEqual(data, f.read())

    def reset_package(self, package_name):
        package = import_module(package_name)
        for entry in package.__path__:
            rmtree(entry)
        self.clean_reload(package)

    def test_spec_from_file_location(self):
        # This is the recommended way to load a module from a known filename
        # (https://docs.python.org/3.8/library/importlib.html#importing-a-source-file-directly).
        def import_from_filename(name, location):
            spec = importlib.util.spec_from_file_location(name, location)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        for name, zip_name, zip_path, attr in [
                ("module1", APP_ZIP, "module1.py", "test_relative"),
                ("module1_renamed", APP_ZIP, "module1.py", "test_relative"),
                ("android1", APP_ZIP, "android1/__init__.py", "x"),
                ("murmurhash", REQS_COMMON_ZIP, "murmurhash/__init__.py", "get_include"),
                ("murmurhash.about", REQS_COMMON_ZIP, "murmurhash/about.py", "__license__")]:
            with self.subTest(name=name):
                module = import_from_filename(name, asset_path(zip_name, zip_path))
                self.assertEqual(name, module.__name__)
                self.assertTrue(hasattr(module, attr))

        bad_path = asset_path(APP_ZIP, "nonexistent.py")
        with self.assertRaisesRegex(FileNotFoundError, bad_path):
            import_from_filename("nonexistent", bad_path)

    # The original importlib.resources API was deprecated in Python 3.11, but its
    # replacement isn't available until Python 3.9.
    #
    # This API cannot access subdirectories within packages.
    def test_importlib_resources(self):
        with catch_warnings():
            filterwarnings("default", category=DeprecationWarning)

            # App ZIP
            pkg = "android1"
            names = ["subdir", "__init__.py", "a.txt", "b.so", "mod1.py"]
            self.assertCountEqual(names, resources.contents(pkg))
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(resources.is_resource(pkg, name),
                                     name != "subdir")

            self.check_ir_resource(APP_ZIP, pkg, "__init__.py", b"# This package is")
            self.check_ir_resource(APP_ZIP, pkg, "a.txt", b"alpha\n")
            self.check_ir_resource(APP_ZIP, pkg, "b.so", b"bravo\n")

            self.assertFalse(resources.is_resource(pkg, "invalid.py"))
            with self.assertRaisesRegex(FileNotFoundError, "invalid.py"):
                resources.read_binary(pkg, "invalid.py")
            with self.assertRaisesRegex(FileNotFoundError, "invalid.py"):
                with resources.path(pkg, "invalid.py"):
                    pass

            # Requirements ZIP
            self.reset_package("murmurhash")
            self.assertCountEqual(["include", "tests", "__init__.pxd", "__init__.pyc", "about.pyc",
                                   "mrmr.pxd", "mrmr.pyx", "mrmr.so"],
                                  resources.contents("murmurhash"))

            self.check_ir_resource(REQS_COMMON_ZIP, "murmurhash", "__init__.pyc", MAGIC_NUMBER)
            self.check_ir_resource(REQS_COMMON_ZIP, "murmurhash", "mrmr.pxd", b"from libc.stdint")
            self.check_ir_resource(REQS_ABI_ZIP, "murmurhash", "mrmr.so", b"\x7fELF")

    def check_ir_resource(self, zip_name, package, filename, start):
        with self.subTest(package=package, filename=filename):
            data = resources.read_binary(package, filename)
            self.assertPredicate(data.startswith, start)

            with resources.path(package, filename) as abs_path:
                if (
                    # Importable files are not extracted to the AssetFinder directory.
                    splitext(filename)[1] in [".py", ".pyc", ".so"]
                    # resources.path() always returns a temporary file on Python >= 3.11.
                    or sys.version_info >= (3, 11)
                ):
                    self.assertEqual(join(str(context.getCacheDir()), "chaquopy/tmp"),
                                     dirname(abs_path))
                else:
                    self.assertEqual(asset_path(zip_name, package.replace(".", "/"), filename),
                                     str(abs_path))
                with open(abs_path, "rb") as f:
                    self.assertEqual(data, f.read())

    def test_importlib_metadata(self):
        dists = list(metadata.distributions())
        self.assertCountEqual(REQUIREMENTS, [d.metadata["Name"] for d in dists])
        for dist in dists:
            dist_info = str(dist._path)
            self.assertPredicate(str.startswith, dist_info, asset_path(REQS_COMMON_ZIP))
            self.assertPredicate(str.endswith, dist_info, ".dist-info")

            # .dist-info directories shouldn't be extracted.
            self.assertNotPredicate(exists, dist_info)

        dist = metadata.distribution("murmurhash")
        self.assertEqual("0.28.0", dist.version)
        self.assertEqual(dist.version, dist.metadata["Version"])
        self.assertIsNone(dist.files)
        self.assertEqual("Matthew Honnibal", dist.metadata["Author"])
        self.assertEqual(["chaquopy-libcxx (>=11000)"], dist.requires)

        # Distribution objects don't implement __eq__.
        def dist_attrs(dist):
            return (dist.version, dist.metadata.items())

        # Check it still works with an unreadable directory on sys.path.
        unreadable_dir = "/"  # Blocked by SELinux.
        try:
            sys.path.insert(0, unreadable_dir)
            self.assertEqual(list(map(dist_attrs, dists)),
                             list(map(dist_attrs, metadata.distributions())))
        finally:
            try:
                sys.path.remove(unreadable_dir)
            except ValueError:
                pass

    @contextmanager
    def assertModifies(self, filename):
        TEST_MTIME = calendar.timegm((2020, 1, 2, 3, 4, 5))
        os.utime(filename, (TEST_MTIME, TEST_MTIME))
        self.assertEqual(TEST_MTIME, os.stat(filename).st_mtime)
        yield
        self.assertNotEqual(TEST_MTIME, os.stat(filename).st_mtime)

    @contextmanager
    def assertNotModifies(self, filename):
        before_stat = os.stat(filename)
        os.chmod(filename, before_stat.st_mode & ~0o222)
        try:
            yield
            after_stat = os.stat(filename)
            self.assertEqual(before_stat.st_mtime, after_stat.st_mtime)
            self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        finally:
            os.chmod(filename, before_stat.st_mode)
