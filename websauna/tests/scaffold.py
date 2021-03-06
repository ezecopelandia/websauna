"""Scaffold test utility functions."""
# Standard Library
import os
import subprocess
import sys
import time
import typing as t
from contextlib import closing
from contextlib import contextmanager
from tempfile import mkdtemp

# SQLAlchemy
import psycopg2

import pytest
from cookiecutter.main import cookiecutter


PYTHON_INTERPRETER = "python{}.{}".format(sys.version_info.major, sys.version_info.minor)


def print_subprocess_fail(worker, cmdline):
    print("{} output:".format(cmdline))
    print(worker.stdout.read().decode("utf-8"))
    print(worker.stderr.read().decode("utf-8"))



def execute_command(cmdline: t.List, folder: str, timeout=5.0):
    """Run a command in a specific folder."""
    worker = subprocess.Popen(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=folder)

    try:
        worker.wait(timeout)
    except subprocess.TimeoutExpired as e:
        print_subprocess_fail(worker, cmdline)
        raise AssertionError("execute_command did not properly exit") from e

    if worker.returncode != 0:
        print_subprocess_fail(worker, cmdline)
        raise AssertionError("scaffold command did not properly exit: {}".format(" ".join(cmdline)))

    return worker.returncode


def execute_venv_command(cmdline, folder, timeout=15.0, wait_and_see=None, assert_exit=0, cd_folder=None):
    """Run a command in a specific folder using virtualenv created there.

    Assume virtualenv is under ``venv`` folder.

    :param wait_and_see: Wait this many seconds to see if app starts up.
    :param assert_exit: Assume exit code is this
    :param cd_folder: cd to this folder before executing the command (relative to folder)
    :return: tuple (exit code, stdout, stderr)
    """

    assert os.path.exists(os.path.join(folder, "venv", "bin", "activate"))

    if type(cmdline) == list:
        cmdline = " ".join(cmdline)

    if cd_folder is not None:
        cd_cmd = "cd {} && ".format(cd_folder)
    else:
        cd_cmd = ""

    cmdline = ". {}/venv/bin/activate ; {} {}".format(folder, cd_cmd, cmdline)

    # print("Executing {} in {}".format(cmdline, folder))

    worker = subprocess.Popen(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=folder, shell=True)

    if wait_and_see is not None:
        time.sleep(wait_and_see)
        worker.poll()

        if worker.returncode is not None:
            # Return code is set if the worker dies within the timeout
            print_subprocess_fail(worker, cmdline)
            raise AssertionError("could not start server like app: {}".format(cmdline))

        worker.kill()
        return 0
    else:
        try:
            worker.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print_subprocess_fail(worker, cmdline)
            raise

    if worker.returncode != assert_exit:
        print_subprocess_fail(worker, cmdline)
        raise AssertionError("venv command did not properly exit: {} in {}. Got exit code {}, assumed {}".format(cmdline, folder, worker.returncode, assert_exit))

    return (worker.returncode, worker.stdout.read().decode("utf-8"), worker.stderr.read().decode("utf-8"))


def preload_wheelhouse(folder:str):
    """Speed up tests by loading Python packages from primed cache.

    Use ``create_wheelhouse.bash`` to prime the cache.

    :param folder: Temporary virtualenv installation
    """
    cache_folder = os.getcwd()

    if os.path.exists(os.path.join(cache_folder, "wheelhouse", "python{}.{}".format(sys.version_info.major, sys.version_info.minor))):
        execute_venv_command("pip install {}/wheelhouse/python{}.{}/*".format(cache_folder, sys.version_info.major, sys.version_info.minor), folder, timeout=3*60)
    else:
        print("No preloaded Python package cache found")


def create_psq_db(request, dbname, dsn=''):
    """py.test fixture to createdb and destroy postgresql database on demand."""
    if not dsn:
        dsn = "dbname=postgres"
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with closing(conn.cursor()) as cursor:
            cursor.execute("SELECT COUNT(*) FROM pg_database WHERE datname='{}'".format(dbname))

            if cursor.fetchone()[0] == 1:
                # Prior interrupted test run
                cursor.execute('DROP DATABASE ' + dbname)

            cursor.execute('CREATE DATABASE ' + dbname)

    def teardown():
        with closing(psycopg2.connect(dsn)) as conn:
            conn.autocommit = True
            with closing(conn.cursor()) as cursor:

                # http://blog.gahooa.com/2010/11/03/how-to-force-drop-a-postgresql-database-by-killing-off-connection-processes/
                cursor.execute("SELECT pg_terminate_backend(pid) from pg_stat_activity where datname='{}';".format(dbname))
                conn.commit()
                cursor.execute("SELECT COUNT(*) FROM pg_database WHERE datname='{}'".format(dbname))
                if cursor.fetchone()[0] == 1:
                    cursor.execute('DROP DATABASE ' + dbname)

    request.addfinalizer(teardown)


@contextmanager
def replace_file(path:str, content:str):
    """A context manager to temporary swap the content of a file.

    :param path: Path to the file
    :param content: New content as a text
    """
    backup = open(path, "rt").read()
    open(path, "wt").write(content)

    try:
        yield None
    finally:
        open(path, "wt").write(backup)


@contextmanager
def insert_content_after_line(path:str, content:str, marker:str):
    """Add piece to text to a text file after a line having a marker string on it."""
    backup = open(path, "rt").read()

    try:
        # Replaces stdout
        out = open(path, "wt")
        for line in backup.split("\n"):

            if marker in line:
                print(content, file=out)

            print(line, file=out)

        out.close()

        yield None

    finally:
        open(path, "wt").write(backup)


@pytest.fixture(scope='session')
def cookiecutter_config(tmpdir_factory) -> str:
    """py.test fixture to generate a tmp config file for cookiecutter.

    :return: Path to cookiecutter config file.
   """
    user_dir = tmpdir_factory.mktemp('user_dir')

    cookiecutters_dir = user_dir.mkdir('cookiecutters')
    replay_dir = user_dir.mkdir('cookiecutter_replay')
    USER_CONFIG = '''cookiecutters_dir: "{cookiecutters_dir}"\nreplay_dir: "{replay_dir}"'''
    config_text = USER_CONFIG.format(
        cookiecutters_dir=cookiecutters_dir,
        replay_dir=replay_dir,
    )
    config_file = user_dir.join('config')

    config_file.write_text(config_text, encoding='utf8')
    return str(config_file)


@pytest.fixture(scope='session')
def app_scaffold(request, cookiecutter_config) -> str:
    """py.test fixture to create app scaffold.

    Create application and virtualenv for it. Run setup.py.

    :return: Path to a temporary folder. In this folder there is `venv` folder and `myapp` folder.
   """

    folder = mkdtemp(prefix="websauna_test_")

    websauna_folder = os.getcwd()
    execute_command([PYTHON_INTERPRETER, "-m", "venv", "venv"], folder, timeout=30)

    # Don't try to push to get a working pip because IT "#€!"#€"#€ DOESNT'T JUST WORK
    # Instead work around any issues caused by missing pip in tests themselves
    #
    # venv fails to install pip under .tox virtualenv due to some obscure bug
    # This broken Python venv stuff drives me crazy... make sure we get  pip
    # pip = os.path.join(folder, "venv", "bin", "pip")
    # if not os.path.exists(pip):
        # Use internal get-pip script to fix broken venv where ensurepip did no give us our shit, because we can't rely on system pip to get this right either
     #   print("The current version of venv/ensurepip modules are broken, fixing problem internally")
     #   get_pip = os.path.join(os.path.dirname(__file__), "get-pip.py")
     #   assert os.path.exists(get_pip)
     #   execute_venv_command("python {} --prefix {}/venv --ignore-installed pip".format(get_pip, folder), folder, timeout=5*60)

    # assert os.path.exists(pip), "Pip not installed: {}".format(pip)

    # PIP cannot handle pip -install .[test]
    # On some systems, the default PIP is too old and it doesn't seem to allow upgrade through wheelhouse
    execute_venv_command("pip install -U pip", folder, timeout=5*60)

    # Install cached PyPi packages
    preload_wheelhouse(folder)

    # Install websauna
    execute_venv_command("cd {} ; pip install -e .[notebook,utils]".format(websauna_folder), folder, timeout=5*60)

    # Create Websauna app, using cookiecutter, from template cookiecutter-websauna-app
    extra_context = {
        'full_name': 'Websauna Team',
        'email': 'developers@websauna.org',
        'company': 'Websauna',
        'github_username': 'websauna',
        'project_name': 'Websauna: News portal',
        'project_short_description': 'Websauna news portal application.',
        'tags': 'python package websauna pyramid',
        'repo_name': 'my.app',
        'namespace': 'my',
        'package_name': 'app',
        'release_date': 'today',
        'year': '2017',
        'version': '1.0.0a1',
        'create_virtualenv': 'No'
    }
    template = 'https://github.com/websauna/cookiecutter-websauna-app/archive/master.zip'
    project_dir = cookiecutter(
        template,
        no_input=True,
        extra_context=extra_context,
        output_dir=folder,
        config_file=cookiecutter_config
    )

    # Install the package created by cookiecutter template
    content_folder = os.path.join(folder, extra_context["repo_name"])
    execute_venv_command("pip install -e {0}".format(content_folder), folder, timeout=5*60)

    def teardown():
        # Clean any processes who still think they want to stick around. Namely: ws-shell doesn't die

        # This kills all processes referring to the temporary folder
        subprocess.call("pkill -SIGKILL -f {}".format(folder), shell=True)

    request.addfinalizer(teardown)
    return folder


def start_ws_pserve(cmdline: str, cwd: str, wait_and_see: float=5.0):
    """Simulate starting ws-pserve command from the command line inside the virtualenv.

    :param cmdline: Command line to run ws-pserve
    :param cwd: Set current workind directory
    :param wait_and_see: Seconds to see if the server comes up
    :return:
    """

    # Clean up all prior processes
    import psutil
    import signal

    # http://stackoverflow.com/a/20691431/315168
    for proc in psutil.process_iter():
        try:
            for conns in proc.connections(kind='inet'):
                if conns.laddr[1] == 6543:
                    print("Killing a proc blocking the port", proc)
                    proc.send_signal(signal.SIGKILL)
                    time.sleep(0.5)
                    continue
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

    # Run ws-pserve inside the virtualenc
    cmdline = ". {}/venv/bin/activate && ".format(cwd) + cmdline

    worker = subprocess.Popen(cmdline, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)

    time.sleep(wait_and_see)
    worker.poll()

    if worker.returncode is not None:
        # Return code is set if the worker dies within the timeout
        print_subprocess_fail(worker, cmdline)
        raise AssertionError("Could not ws-pserve: {}".format(cmdline))

    return worker
