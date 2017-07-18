package com.chaquo.python;


/** Interface to Python.
 *
 * Unless otherwise specified, methods in this class throw {@link PyException} on failure. */
public class Python {

    /** Provides information needed to start Python. */
    public interface Platform {
        /** Returns the value to assign to `PYTHONPATH`. */
        String getPath();
    }

    private static boolean started;
    private static boolean failed;
    private static Python instance;

    /** Gets the interface to Python. This method always returns the same object. If
     * {@link #start start()} has not yet been called, it will be called with a new
     * {@link GenericPlatform}. */
    public static synchronized Python getInstance() {
        if (instance == null) {
            if (!started) {
                start(new GenericPlatform());
            }
            instance = new Python();
        }
        return instance;
    }

    /** Starts the Python virtual machine. If this method is called, it can only be called once, and
     * it must be before any call to {@link #getInstance}.
     *
     * If running on Android, It's important to structure your app so that `Python.start` is always
     * called with an {@link AndroidPlatform} before attempting to access Python. There are two
     * basic ways to achieve this:
     *
     * 1. Call it from {@link android.app.Application#onCreate() Application.onCreate()}, or another
     *    location which is guaranteed to run exactly once per process.
     * 1. Call it conditionally after first checking {@link #isStarted}.
     *
     * If running on any other platform, there's no need to call this method, unless you want to
     * customize the Python startup process.
     **/
    public static synchronized void start(Platform platform) {
        if (started) {
            throw new IllegalStateException("Python already started");
        }
        if (failed) {
            // startNative will crash if called more than once.
            throw new IllegalStateException("Python startup previously failed, and cannot be retried");
        }
        try {
            startNative(platform, platform.getPath());
            started = true;
        } catch (Exception e) {
            failed = true;
            throw e;
        }
    }

    /** Return whether the Python virtual machine is running */
    public static synchronized boolean isStarted() {
        return started;
    }

    /** There is no stop() method, because Py_Finalize does not guarantee an orderly or complete
     * cleanup. */
    private static native void startNative(Platform platform, String pythonPath);

    // =======================================================================

    private Python() {}

    /** Returns the module with the given absolute name. */
    public native PyObject getModule(String name);

    /** Returns the module `__builtin__` in Python 2 or `builtins` in Python 3. This module contains
     * Python's built-in functions (e.g. `open`), types (e.g. `dict`), constants (e.g. `True`) and
     * exceptions (e.g. `ValueError`). */
    public native PyObject getBuiltins();
}
