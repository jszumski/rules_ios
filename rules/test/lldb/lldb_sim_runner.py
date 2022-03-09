# This program is a library for constructing iOS LLDB command line automation,
# oriented around testing. It orchestrates a simulator thread, LLDB thread, to
# run simulators and debuggers concurrently. see run_lldb_test
import rules.test.lldb.sim_template as sim_template
import subprocess
import os
import logging
import time
import threading
import traceback
import tempfile
import shutil
import json

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO)
logger = logging.getLogger(__name__)


def find_pid(app_name, udid):
    """ Find the PID for the app and the udid of the simulator"""
    system_processes = subprocess.Popen(
        ['ps', '-aU', '0'], stdout=subprocess.PIPE).communicate()[0]
    out_pid = None
    needle = app_name + ".app"
    for misc_p in system_processes.decode("utf8").split("\n"):
        if needle in misc_p and udid in misc_p:
            pid_line = misc_p.strip()
            accum = ""
            for c in pid_line:
                if c == " ":
                    break
                else:
                    accum += c
            return int(accum)
    return None


def boot_simulator(developer_path, simctl_path, udid):
    """Launches the iOS simulator for the given identifier.

    Unlike the rules_apple runner we don't foreground it because of concurrency
    problems
    """
    logger.info("Launching simulator with udid: %s", udid)
    subprocess.run(
        ["xcrun", "simctl", "boot",  udid],
        check=True)
    logger.debug("Simulator launched.")
    if not sim_template.wait_for_sim_to_boot(simctl_path, udid):
        raise Exception("Failed to launch simulator with UDID: " + udid)


def run_app_in_simulator(ctx, simulator_udid, developer_path, simctl_path,
                         ios_application_output_path, app_name):
    """Installs and runs an app in the specified simulator.
    """
    logger.info("Booting simulator App with path %s", developer_path)
    try:
        boot_simulator(
            developer_path, simctl_path, simulator_udid)
    except:
        logger.info(
            "Second attempt to boot simulator App with path %s", developer_path)
        # This is a hack - when rapidly iterating locally it can fail with:
        # Simulator.app cannot be opened for an unexpected reason,
        # error=Error Domain=NSOSStatusErrorDomain Code=-600 "procNotFound: no
        # eligible process with specified descriptor" UserInfo={_LSLine=379,
        # _LSFunction=_LSAnnotateAndSendAppleEventWithOptions}
        boot_simulator(
            developer_path, simctl_path, simulator_udid)

    with sim_template.extracted_app(ios_application_output_path, app_name) as app_path:
        logger.debug("Installing app %s to simulator %s",
                     app_path, simulator_udid)
        subprocess.run([simctl_path, "install", simulator_udid, app_path],
                       check=True)
        app_bundle_id = sim_template.bundle_id(app_path)
        logger.info("Launching app %s in simulator %s", app_bundle_id,
                    simulator_udid)
        args = [
            simctl_path, "launch", "--wait-for-debugger", simulator_udid, app_bundle_id
        ]

        # This returns the pid of the process
        simctl_process = subprocess.Popen(args, env=sim_template.simctl_launch_environ(
        ),  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sim_pid = simctl_process.pid

        # Stream the simctl output to stdout, consider moving to a file
        reader_tstdout = threading.Thread(
            target=monitor_output, args=(simctl_process.stdout, "simctl"))
        reader_tstdout.daemon = True
        reader_tstdout.start()

        reader_tstderr = threading.Thread(
            target=monitor_output, args=(simctl_process.stderr, "simctl"))
        reader_tstderr.daemon = True
        reader_tstderr.start()

        logger.info("Find simctl PID %s", sim_pid)

        # After it boots, notify the `ctx` by calling SetStartedAppPid
        app_started_pid = None
        while simctl_process.returncode == None and app_started_pid == None:
            if not app_started_pid:
                app_started_pid = find_pid(ctx.app_name, simulator_udid)
                if app_started_pid:
                    logger.info("Got PID %s", app_started_pid)
                    time.sleep(1)
                    ctx.SetStartedAppPid(app_started_pid)

            logger.info("Poll simulator %s", simctl_process.poll())
            time.sleep(1)

        logger.info("Sim exited return code %d", simctl_process.returncode)
        if simctl_process.returncode != 0:
            ctx.fail()


def sim_thread_main(ctx, sim_device, sim_os_version, ios_application_output_path, app_name,
                    minimum_os):
    xcode_select_result = subprocess.run(["xcode-select", "-p"],
                                         encoding="utf-8",
                                         check=True,
                                         stdout=subprocess.PIPE)
    developer_path = xcode_select_result.stdout.rstrip()
    simctl_path = os.path.join(developer_path, "usr", "bin", "simctl")
    with sim_template.ios_simulator(simctl_path, minimum_os, sim_device,
                                    sim_os_version) as simulator_udid:
        run_app_in_simulator(ctx, simulator_udid, developer_path, simctl_path,
                             ios_application_output_path, app_name)


def sim_thread_entry(ctx, device, sdk, ipa_path):
    try:
        sim_thread_main(ctx, device, sdk, ipa_path, ctx.app_name, sdk)
    except Exception:
        traceback.print_exc()
        ctx.Fail()


ctxlock = threading.RLock()


class TestContext():
    def __init__(self, pid, app_name, test_root):
        self.app_name = app_name
        self.test_root = test_root

        # Not thread safe
        self.status = None
        self.pid = None

    def SetStartedAppPid(self, app_pid):
        with ctxlock:
            self.pid = app_pid

    def GetStartedAppPid(self):
        with ctxlock:
            return self.pid

    def Fail(self):
        traceback.print_exc()
        logger.error("FAIL")
        with ctxlock:
            self.status = -1

    # Returns None for in progress, -1 fail, 1 pass
    def GetCompletionStatus(self):
        with ctxlock:
            return self.status

    def SetLLDBCompletionStatus(self, status):
        with ctxlock:
            self.status = status
        if status != 0:
            self.Fail()

    def GetTestRoot(self):
        return self.test_root


def lldb_thread_entry(ctx, lldbinit):
    while not ctx.GetStartedAppPid() and ctx.GetCompletionStatus() == None:
        logger.info(
            "Waiting for %s.app to post to start debugger", ctx.app_name)
        time.sleep(1)
    try:
        attach_debugger(ctx, test_root=ctx.GetTestRoot(),
                        pid=ctx.GetStartedAppPid(), lldbinit=lldbinit)
    except Exception:
        traceback.print_exc()
        ctx.Fail()


def monitor_output(out, prefix, dupe_file_path=None):
    # Copy these lines to a file
    dupe_file = open(dupe_file_path, "w") if dupe_file_path else None
    for line in iter(out.readline, b''):
        str_line = line.decode("utf8")
        logger.info(prefix + " " + str_line.rstrip("\n"))
        if dupe_file:
            dupe_file.write(str_line)

    out.close()
    if dupe_file:
        dupe_file.close()

    logger.info(prefix + " output stream Closed")


def attach_debugger(ctx, test_root, pid, lldbinit):
    # TODO: ideally use Bazel's configured Xcode's LLDB if it exists
    args = ["xcrun", "lldb", "-p", str(pid)]

    logger.info("spawning LLDB with args %s cwd=%s", str(args), test_root)
    lldb_process = subprocess.Popen(
        args, cwd=test_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Stream the LLDB output to stdout, consider moving to a file
    stdout_path = os.path.join(test_root, "lldb.stdout")
    reader_t = threading.Thread(
        target=monitor_output, args=(lldb_process.stdout, "LLDB", stdout_path))
    reader_t.daemon = True
    reader_t.start()

    reader_tstderr = threading.Thread(
        target=monitor_output, args=(lldb_process.stderr, "LLDB"))
    reader_tstderr.daemon = True
    reader_tstderr.start()

    # Source the users lldbinit
    lldb_process.stdin.write(
        ("command source " + str(lldbinit) + "\n").encode('utf-8'))
    lldb_process.stdin.flush()

    # Consider validating this here
    while lldb_process.poll() == None:
        logger.info("Poll LLDB %s", lldb_process.poll())
        time.sleep(1)

    logger.info("LLDB Completed return code %d", lldb_process.returncode)
    ctx.SetLLDBCompletionStatus(lldb_process.returncode)


def run_lldb(ipa_path, sdk, device, lldbinit_path, test_root):
    """ Spawns a simulator with the `ipa_path`, `sdk`, device wit the `.lldbinit`

        It waits for LLDB to exit and retuns the exit code

        stdout is written to lldb.stdout in the test_root
    """
    if not os.path.exists(ipa_path) or not ipa_path.endswith(".ipa"):
        raise Exception(f"Missing IPA / [ --app ] %s", ipa_path)

    # Consider handling other IPAs here or types
    ipa_name = os.path.basename(ipa_path)
    app_name = ipa_name.replace(".ipa", "")

    if not sdk:
        raise Exception(f"Missing SDK / [ --sdk ]")

    if not device:
        raise Exception(f"Missing device / [ --device ] ")

    if not os.path.exists(lldbinit_path):
        raise Exception(f"Missing lldbinit / [ --lldbinit]", lldbinit_path)

    exit_code = None
    ctx = None
    logger.info("Got app name %s", app_name)
    try:
        ctx = TestContext(None, app_name, str(test_root))

        # Main runloop - polls completion status
        sim_thread = threading.Thread(
            target=sim_thread_entry, args=(ctx, device, sdk, ipa_path))
        debugger_thread = threading.Thread(
            target=lldb_thread_entry, args=(ctx, lldbinit_path))
        sim_thread.start()
        debugger_thread.start()
        while ctx.GetCompletionStatus() == None:
            logger.debug('Main thread...')
            time.sleep(1)
        sim_thread.join()
        debugger_thread.join()
        exit_code = ctx.GetCompletionStatus()
    except Exception:
        traceback.print_exc()

    if exit_code != 0:
        raise Exception(f"LLDB exited with non-zero status %d", exit_code)
    return exit_code
