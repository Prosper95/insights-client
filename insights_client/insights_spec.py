import os
import re
from subprocess import Popen, PIPE, STDOUT
import errno
import shlex
import logging
import six
from tempfile import NamedTemporaryFile
from utilities import determine_hostname
from constants import InsightsConstants as constants

logger = logging.getLogger(constants.app_name)


class InsightsSpec(object):
    '''
    A spec loaded from the uploader.json
    '''
    def __init__(self, spec, exclude):
        # exclusions patterns for this spec
        self.exclude = exclude
        # pattern for spec collection
        self.pattern = spec['pattern'] if spec['pattern'] else None
        # absolute destination inside the archive for this spec
        self.archive_path = spec['archive_file_name']


class InsightsCommand(InsightsSpec):
    '''
    A command spec
    '''
    def __init__(self, spec, exclude, mountpoint, target_name):
        InsightsSpec.__init__(self, spec, exclude)
        # substitute mountpoint for collection
        # have to use .replace instead of .format because there are other
        #  braced keys in the collection spec not used here
        self.command = spec['command'].replace(
            '{CONTAINER_MOUNT_POINT}', mountpoint).replace(
            '{DOCKER_IMAGE_NAME}', target_name).replace(
            '{DOCKER_CONTAINER_NAME}', target_name)
        self.mangled_command = self._mangle_command(self.command)
        # have to re-mangle archive path in case there's a pre-command arg
        self.archive_path = os.path.join(
            os.path.dirname(self.archive_path), self.mangled_command)
        if not six.PY3:
            self.command = self.command.encode('utf-8', 'ignore')
        self.black_list = ['rm', 'kill', 'reboot', 'shutdown']

    def _mangle_command(self, command, name_max=255):
        """
        Mangle the command name, lifted from sos
        """
        mangledname = re.sub(r"^/(usr/|)(bin|sbin)/", "", command)
        mangledname = re.sub(r"[^\w\-\.\/]+", "_", mangledname)
        mangledname = re.sub(r"/", ".", mangledname).strip(" ._-")
        mangledname = mangledname[0:name_max]
        return mangledname

    def get_output(self):
        '''
        Execute a command through system shell. First checks to see if
        the requested command is executable. Returns (returncode, stdout, 0)
        '''
        # ensure consistent locale for collected command output
        cmd_env = {'LC_ALL': 'C'}
        args = shlex.split(self.command)

        # never execute this stuff
        if set.intersection(set(args), set(self.black_list)):
            raise RuntimeError("Command Blacklist")

        try:
            logger.debug('Executing: %s', args)
            proc0 = Popen(args, shell=False, stdout=PIPE, stderr=STDOUT,
                          bufsize=-1, env=cmd_env, close_fds=True)
        except OSError as err:
            if err.errno == errno.ENOENT:
                logger.debug('Command %s not found', self.command)
                return
            else:
                raise err

        dirty = False

        cmd = "/bin/sed -rf " + constants.default_sed_file
        sedcmd = Popen(shlex.split(cmd.encode('utf-8')),
                       stdin=proc0.stdout,
                       stdout=PIPE)
        proc0.stdout.close()
        proc0 = sedcmd

        if self.exclude is not None:
            exclude_file = NamedTemporaryFile()
            exclude_file.write("\n".join(self.exclude))
            exclude_file.flush()
            cmd = "/bin/grep -F -v -f %s" % exclude_file.name
            proc1 = Popen(shlex.split(cmd.encode("utf-8")),
                          stdin=proc0.stdout,
                          stdout=PIPE)
            proc0.stdout.close()
            if self.pattern is None or len(self.pattern) == 0:
                stdout, stderr = proc1.communicate()
            proc0 = proc1
            dirty = True

        if self.pattern is not None and len(self.pattern):
            pattern_file = NamedTemporaryFile()
            pattern_file.write("\n".join(self.pattern))
            pattern_file.flush()
            cmd = "/bin/grep -F -f %s" % pattern_file.name
            proc2 = Popen(shlex.split(cmd.encode("utf-8")),
                          stdin=proc0.stdout,
                          stdout=PIPE)
            proc0.stdout.close()
            stdout, stderr = proc2.communicate()
            dirty = True

        if not dirty:
            stdout, stderr = proc0.communicate()

        # Required hack while we still pass shell=True to Popen; a Popen
        # call with shell=False for a non-existant binary will raise OSError.
        if proc0.returncode == 126 or proc0.returncode == 127:
            stdout = "Could not find cmd: %s", self.command

        logger.debug("Status: %s", proc0.returncode)
        logger.debug("stderr: %s", stderr)
        return stdout.decode('utf-8', 'ignore')


class InsightsFile(InsightsSpec):
    '''
    A file spec
    '''
    def __init__(self, spec, exclude, mountpoint, target_name):
        InsightsSpec.__init__(self, spec, exclude)
        # substitute mountpoint for collection
        self.real_path = spec['file'].replace(
            '{CONTAINER_MOUNT_POINT}', mountpoint).replace(
            '{DOCKER_IMAGE_NAME}', target_name).replace(
            '{DOCKER_CONTAINER_NAME}', target_name)
        self.relative_path = spec['file'].replace(
            '{CONTAINER_MOUNT_POINT}', '').replace(
            '{DOCKER_IMAGE_NAME}', target_name).replace(
            '{DOCKER_CONTAINER_NAME}', target_name)
        self.archive_path = self.archive_path.replace('{EXPANDED_FILE_NAME}', self.real_path)

    def get_output(self):
        '''
        Get file content, selecting only lines we are interested in
        '''
        if not os.path.isfile(self.real_path):
            logger.debug('File %s does not exist', self.real_path)
            return

        logger.debug('Copying %s to %s with filters %s',
                     self.real_path, self.archive_path, str(self.pattern))

        cmd = []
        cmd.append("/bin/sed".encode('utf-8'))
        cmd.append("-rf".encode('utf-8'))
        cmd.append(constants.default_sed_file.encode('utf-8'))
        cmd.append(self.real_path.encode('utf8'))
        sedcmd = Popen(cmd,
                       stdout=PIPE)

        if self.exclude is not None:
            exclude_file = NamedTemporaryFile()
            exclude_file.write("\n".join(self.exclude))
            exclude_file.flush()

            cmd = "/bin/grep -v -F -f %s" % exclude_file.name
            args = shlex.split(cmd.encode("utf-8"))
            proc = Popen(args, stdin=sedcmd.stdout, stdout=PIPE)
            sedcmd.stdout.close()
            stdin = proc.stdout
            if self.pattern is None:
                output = proc.communicate()[0]
            else:
                sedcmd = proc

        if self.pattern is not None:
            pattern_file = NamedTemporaryFile()
            pattern_file.write("\n".join(self.pattern))
            pattern_file.flush()

            cmd = "/bin/grep -F -f %s" % pattern_file.name
            args = shlex.split(cmd.encode("utf-8"))
            proc1 = Popen(args, stdin=sedcmd.stdout, stdout=PIPE)
            sedcmd.stdout.close()

            if self.exclude is not None:
                stdin.close()

            output = proc1.communicate()[0]

        if self.pattern is None and self.exclude is None:
            output = sedcmd.communicate()[0]

        return output.decode('utf-8', 'ignore').strip()
