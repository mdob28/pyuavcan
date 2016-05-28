#
# Copyright (C) 2014-2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Ben Dyer <ben_dyer@mac.com>
#         Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import division, absolute_import, print_function, unicode_literals
import os
import sys
import time
import inspect
import binascii
import select
import multiprocessing
import threading
import copy
from logging import getLogger
from .common import DriverError, TxQueueFullError, CANFrame, AbstractDriver
from .timestamp_estimator import TimestampEstimator

try:
    import queue
except ImportError:
    # noinspection PyPep8Naming,PyUnresolvedReferences
    import Queue as queue

logger = getLogger(__name__)

# If PySerial isn't available, we can't support SLCAN
try:
    import serial
except ImportError:
    serial = None
    logger.info("Cannot import PySerial; SLCAN will not be available.")

try:
    # noinspection PyUnresolvedReferences
    sys.getwindowsversion()
    RUNNING_ON_WINDOWS = True
except AttributeError:
    RUNNING_ON_WINDOWS = False


#
# Constants and defaults
#
RX_QUEUE_SIZE = 100000
TX_QUEUE_SIZE = 1000

TIMESTAMP_OVERFLOW_PERIOD = 60          # Defined by SLCAN protocol

DEFAULT_BITRATE = 1000000
DEFAULT_BAUDRATE = 3000000

ACK_TIMEOUT = 0.5
ACK = b'\r'
NACK = b'\x07'
CLI_END_OF_LINE = b'\r\n'
CLI_END_OF_TEXT = b'\x03'

DEFAULT_MAX_ADAPTER_CLOCK_RATE_ERROR_PPM = 200      # Suits virtually all adapters
DEFAULT_FIXED_RX_DELAY = 0.0002                     # Good for USB, could be higher for UART
DEFAULT_MAX_ESTIMATED_RX_DELAY_TO_RESYNC = 0.1      # When clock divergence exceeds this value, resync

IO_PROCESS_INIT_TIMEOUT = 5
IO_PROCESS_NICENESS_INCREMENT = -18

MAX_SUCCESSIVE_ERRORS_TO_GIVE_UP = 1000


#
# IPC entities
#
IPC_SIGNAL_INIT_OK = 'init_ok'                     # Sent from IO process to the parent process when init is done
IPC_COMMAND_STOP = 'stop'                          # Sent from parent process to the IO process when it's time to exit


class IPCCommandLineExecutionRequest:
    DEFAULT_TIMEOUT = 1

    def __init__(self, command, timeout=None):
        if isinstance(command, bytes):
            command = command.decode('utf8')
        self.command = command.strip()
        self.monotonic_deadline = time.monotonic() + (timeout or self.DEFAULT_TIMEOUT)

    @property
    def expired(self):
        return time.monotonic() >= self.monotonic_deadline


class IPCCommandLineExecutionResponse:
    def __init__(self, command, lines=None, expired=False):
        def try_decode(what):
            if isinstance(what, bytes):
                return what.decode('utf8')
            return what

        self.command = try_decode(command).strip()
        self.lines = [try_decode(ln) for ln in (lines or [])]
        self.expired = expired

    def __str__(self):
        if not self.expired:
            return '%r %r' % (self.command, self.lines)
        else:
            return '%r EXPIRED' % self.command

    __repr__ = __str__


_pending_command_line_execution_requests = queue.Queue()


#
# Logic of the IO process
#
class RxWorker:
    PY2_COMPAT = sys.version_info[0] < 3
    SELECT_TIMEOUT = 0.1
    READ_BUFFER_SIZE = 1024 * 8             # Arbitrary large number

    def __init__(self, conn, rx_queue, ts_estimator_mono, ts_estimator_real, termination_condition):
        self._conn = conn
        self._output_queue = rx_queue
        self._ts_estimator_mono = ts_estimator_mono
        self._ts_estimator_real = ts_estimator_real
        self._termination_condition = termination_condition

        if RUNNING_ON_WINDOWS:
            # select() doesn't work on serial ports under Windows, so we have to resort to workarounds. :(
            self._conn.timeout = self.SELECT_TIMEOUT
        else:
            self._conn.timeout = 0

    def _read_port(self):
        if RUNNING_ON_WINDOWS:
            data = self._conn.read(max(1, self._conn.inWaiting()))
            # Timestamping as soon as possible after unblocking
            ts_mono = time.monotonic()
            ts_real = time.time()
        else:
            select.select([self._conn.fileno()], [], [], self.SELECT_TIMEOUT)
            # Timestamping as soon as possible after unblocking
            ts_mono = time.monotonic()
            ts_real = time.time()
            # Read as much data as possible in order to avoid RX overrun
            data = self._conn.read(self.READ_BUFFER_SIZE)
        return data, ts_mono, ts_real

    def _process_slcan_line(self, line, local_ts_mono, local_ts_real):
        line = line.strip()
        line_len = len(line)

        if line_len < 1:
            return

        # Checking the header, ignore all irrelevant lines
        if line[0] == b'T'[0]:
            id_len = 8
        elif line[0] == b't'[0]:
            id_len = 3
        else:
            return

        # Parsing ID and DLC
        packet_id = int(line[1:1 + id_len], 16)
        if self.PY2_COMPAT:
            packet_len = int(line[1 + id_len])              # This version is horribly slow
        else:
            packet_len = line[1 + id_len] - 48              # Py3 version is faster

        if packet_len > 8 or packet_len < 0:
            raise DriverError('Invalid packet length')

        # Parsing the payload, detecting timestamp
        # <type> <id> <dlc> <data>         [timestamp]
        # 1      3|8  1     packet_len * 2 [4]
        total_length = 2 + id_len + packet_len * 2
        if line_len > total_length:
            with_timestamp = True
            total_length += 4
        else:
            with_timestamp = False

        if line_len != total_length:
            raise DriverError('Unexpected length of SLCAN line')

        packet_data = binascii.a2b_hex(line[2 + id_len:2 + id_len + packet_len * 2])

        # Handling the timestamp, if present
        if with_timestamp:
            ts_hardware = int(line[-4:], 16) * 1e-3
            ts_mono = self._ts_estimator_mono.update(ts_hardware, local_ts_mono)
            ts_real = self._ts_estimator_real.update(ts_hardware, local_ts_real)
        else:
            ts_mono = local_ts_mono
            ts_real = local_ts_real

        frame = CANFrame(packet_id, packet_data, (id_len == 8), ts_monotonic=ts_mono, ts_real=ts_real)
        self._output_queue.put_nowait(frame)

    def _process_many_slcan_lines(self, lines, ts_mono, ts_real):
        for slc in lines:
            # noinspection PyBroadException
            try:
                self._process_slcan_line(slc, local_ts_mono=ts_mono, local_ts_real=ts_real)
            except Exception:
                logger.error('Could not process SLCAN line %r', slc, exc_info=True)

    # noinspection PyBroadException
    def run(self):
        logger.info('RX worker started')

        successive_errors = 0
        data = bytes()

        outstanding_command = None
        outstanding_command_response_lines = []

        while not self._termination_condition():
            try:
                new_data, ts_mono, ts_real = self._read_port()
                data += new_data

                # Checking the command queue and handling command timeouts
                if outstanding_command is None:
                    try:
                        outstanding_command = _pending_command_line_execution_requests.get_nowait()
                        outstanding_command_response_lines = []
                    except queue.Empty:
                        pass

                if outstanding_command is not None:
                    if outstanding_command.expired:
                        response = IPCCommandLineExecutionResponse(outstanding_command.command, expired=True)
                        self._output_queue.put_nowait(response)
                        # Next command will be fetched on the next iteration
                        outstanding_command = None
                        outstanding_command_response_lines = []

                # Processing in normal mode if there's no outstanding command; using much slower CLI mode otherwise
                if outstanding_command is None:
                    slcan_lines = data.split(ACK)
                    slcan_lines, data = slcan_lines[:-1], slcan_lines[-1]

                    self._process_many_slcan_lines(slcan_lines, ts_mono=ts_mono, ts_real=ts_real)
                    del slcan_lines
                else:
                    # TODO This branch contains dirty and poorly tested code. Refactor once the protocol matures.
                    split_lines = data.split(CLI_END_OF_LINE)
                    split_lines, data = split_lines[:-1], split_lines[-1]

                    # Processing the mix of SLCAN and CLI lines
                    for ln in split_lines:
                        tmp = ln.split(ACK)
                        slcan_lines, cli_line = tmp[:-1], tmp[-1].strip()

                        self._process_many_slcan_lines(slcan_lines, ts_mono=ts_mono, ts_real=ts_real)

                        # Processing the CLI line
                        logger.debug('Processing CLI response line %r as...', cli_line)
                        if len(outstanding_command_response_lines) == 0:
                            if outstanding_command is not None and \
                                    cli_line == outstanding_command.command.encode('utf8'):
                                logger.debug('...echo')
                                outstanding_command_response_lines.append(cli_line)
                            else:
                                # Otherwise we're receiving some CLI garbage before or after the command output, e.g.
                                # end of the previous command output if it was missed
                                logger.debug('...garbage')
                        else:
                            if cli_line == CLI_END_OF_TEXT:
                                logger.debug('...end-of-text')
                                # Shipping
                                response = IPCCommandLineExecutionResponse(outstanding_command.command,
                                                                           lines=outstanding_command_response_lines[1:])
                                self._output_queue.put_nowait(response)
                                # Immediately fetching the next command, expiration is not checked
                                try:
                                    outstanding_command = _pending_command_line_execution_requests.get_nowait()
                                except queue.Empty:
                                    outstanding_command = None
                                outstanding_command_response_lines = []
                            else:
                                logger.debug('...mid response')
                                outstanding_command_response_lines.append(cli_line)

                    del split_lines

                    # The remainder may contain SLCAN and CLI lines as well;
                    # there is no reason not to process SLCAN ones immediately.
                    # The last byte could be beginning of an \r\n sequence, so it's excluded from parsing.
                    data, last_byte = data[:-1], data[-1:]
                    slcan_lines = data.split(ACK)
                    slcan_lines, data = slcan_lines[:-1], slcan_lines[-1] + last_byte

                    self._process_many_slcan_lines(slcan_lines, ts_mono=ts_mono, ts_real=ts_real)

                successive_errors = 0
            except Exception as ex:
                # TODO: handle the case when the port is closed
                logger.error('RX thread error %d of %d',
                             successive_errors, MAX_SUCCESSIVE_ERRORS_TO_GIVE_UP, exc_info=True)

                try:
                    self._output_queue.put_nowait(ex)
                except Exception:
                    pass

                successive_errors += 1
                if successive_errors >= MAX_SUCCESSIVE_ERRORS_TO_GIVE_UP:
                    break

        logger.info('RX worker is stopping')


class TxWorker:
    QUEUE_BLOCK_TIMEOUT = 0.1

    def __init__(self, conn, rx_queue, tx_queue, termination_condition):
        self._conn = conn
        self._rx_queue = rx_queue
        self._tx_queue = tx_queue
        self._termination_condition = termination_condition

    def _send_frame(self, frame):
        line = '%s%d%s\r' % (('T%08X' if frame.extended else 't%03X') % frame.id,
                             len(frame.data),
                             binascii.b2a_hex(frame.data).decode('ascii'))

        self._conn.write(line.encode('ascii'))
        self._conn.flush()

    def _execute_command(self, command):
        logger.debug('Executing command line %r', command.command)
        self._conn.write(command.command.encode('ascii') + CLI_END_OF_LINE)
        self._conn.flush()
        _pending_command_line_execution_requests.put(command)

    def run(self):
        while True:
            try:
                command = self._tx_queue.get(True, self.QUEUE_BLOCK_TIMEOUT)

                if isinstance(command, CANFrame):
                    self._send_frame(command)
                elif isinstance(command, IPCCommandLineExecutionRequest):
                    self._execute_command(command)
                elif command == IPC_COMMAND_STOP:
                    break
                else:
                    raise DriverError('IO process received unknown IPC command: %r' % command)
            except queue.Empty:
                # Checking in this handler in order to avoid interference with traffic
                if self._termination_condition():
                    break
            except Exception as ex:
                logger.error('TX thread exception', exc_info=True)
                # Propagating the exception to the parent process
                # noinspection PyBroadException
                try:
                    self._rx_queue.put_nowait(ex)
                except Exception:
                    pass


# noinspection PyUnresolvedReferences
def _raise_self_process_priority():
    if RUNNING_ON_WINDOWS:
        import win32api
        import win32process
        import win32con
        handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, True, win32api.GetCurrentProcessId())
        win32process.SetPriorityClass(handle, win32process.REALTIME_PRIORITY_CLASS)
    else:
        import os
        os.nice(IO_PROCESS_NICENESS_INCREMENT)


def _init_adapter(conn, bitrate):
    def _wait_for_ack():
        conn.timeout = ACK_TIMEOUT
        while True:
            b = conn.read(1)
            if not b:
                raise DriverError('SLCAN ACK timeout')
            if b == NACK:
                raise DriverError('SLCAN NACK in response')
            if b == ACK:
                break

    speed_code = {
        1000000: 8,
        800000: 7,
        500000: 6,
        250000: 5,
        125000: 4,
        100000: 3,
        50000: 2,
        20000: 1,
        10000: 0
    }[bitrate if bitrate is not None else DEFAULT_BITRATE]

    num_retries = 3
    while True:
        try:
            # Sending an empty command in order to reset the adapter's command parser, then discarding all output
            conn.write(b'\r')
            try:
                _wait_for_ack()
            except DriverError:
                pass
            time.sleep(0.1)
            conn.flushInput()

            # Making sure the channel is closed - some adapters may refuse to re-open if the channel is already open
            conn.write(b'C\r')
            try:
                _wait_for_ack()
            except DriverError:
                pass

            # Setting speed code
            conn.write(('S%d\r' % speed_code).encode())
            conn.flush()
            _wait_for_ack()

            # Opening the channel
            conn.write(b'O\r')
            conn.flush()
            _wait_for_ack()

            # Clearing error flags
            conn.write(b'F\r')
            conn.flush()
            _wait_for_ack()
        except Exception as ex:
            if num_retries > 0:
                logger.error('Could not init SLCAN adapter, will retry; error was: %s', ex, exc_info=True)
            else:
                raise ex
            num_retries -= 1
        else:
            break

    # Discarding all input again
    time.sleep(0.1)
    conn.flushInput()


def _stop_adapter(conn):
    conn.write(b'C\r' * 10)
    conn.flush()


# noinspection PyBroadException
def _io_process(device,
                tx_queue,
                rx_queue,
                parent_pid,
                bitrate=None,
                baudrate=None,
                max_adapter_clock_rate_error_ppm=None,
                fixed_rx_delay=None,
                max_estimated_rx_delay_to_resync=None):
    logger.info('IO process started with PID %r', os.getpid())

    # We don't need stdin
    try:
        stdin_fileno = sys.stdin.fileno()
        sys.stdin.close()
        os.close(stdin_fileno)
    except Exception:
        pass

    def is_parent_process_alive():
        # Works on any platform
        try:
            os.kill(parent_pid, 0)
            return True
        except OSError:
            logger.info('Parent process is dead')
            return False

    try:
        _raise_self_process_priority()
    except Exception as ex:
        logger.warning('Could not adjust priority of the IO process: %r', ex)

    #
    # This is needed to convert timestamps from hardware clock to local clocks
    #
    if max_adapter_clock_rate_error_ppm is None:
        max_adapter_clock_rate_error = DEFAULT_MAX_ADAPTER_CLOCK_RATE_ERROR_PPM / 1e6
    else:
        max_adapter_clock_rate_error = max_adapter_clock_rate_error_ppm / 1e6

    fixed_rx_delay = fixed_rx_delay if fixed_rx_delay is not None else DEFAULT_FIXED_RX_DELAY
    max_estimated_rx_delay_to_resync = max_estimated_rx_delay_to_resync or DEFAULT_MAX_ESTIMATED_RX_DELAY_TO_RESYNC

    ts_estimator_mono = TimestampEstimator(max_rate_error=max_adapter_clock_rate_error,
                                           source_clock_overflow_period=TIMESTAMP_OVERFLOW_PERIOD,
                                           fixed_delay=fixed_rx_delay,
                                           max_phase_error_to_resync=max_estimated_rx_delay_to_resync)
    ts_estimator_real = copy.deepcopy(ts_estimator_mono)

    #
    # Preparing the RX thread
    #
    should_exit = False

    def rx_thread_wrapper():
        rx_worker = RxWorker(conn=conn,
                             rx_queue=rx_queue,
                             ts_estimator_mono=ts_estimator_mono,
                             ts_estimator_real=ts_estimator_real,
                             termination_condition=lambda: should_exit)
        try:
            rx_worker.run()
        except Exception as ex:
            logger.error('RX thread failed, exiting', exc_info=True)
            # Propagating the exception to the parent process
            rx_queue.put_nowait(ex)

    rxthd = threading.Thread(target=rx_thread_wrapper, name='slcan_rx')
    rxthd.daemon = True

    try:
        conn = serial.Serial(device, baudrate or DEFAULT_BAUDRATE)
    except Exception as ex:
        logger.error('Could not open port', exc_info=True)
        rx_queue.put_nowait(ex)
        return

    #
    # Actual work is here
    #
    try:
        _init_adapter(conn, bitrate)

        rxthd.start()

        logger.info('IO process initialization complete')
        rx_queue.put(IPC_SIGNAL_INIT_OK)

        tx_worker = TxWorker(conn=conn,
                             rx_queue=rx_queue,
                             tx_queue=tx_queue,
                             termination_condition=lambda: (should_exit or not
                                                            rxthd.is_alive() or not
                                                            is_parent_process_alive()))
        tx_worker.run()
    finally:
        logger.info('IO process is terminating...')
        should_exit = True
        if rxthd.is_alive():
            rxthd.join()

        _stop_adapter(conn)
        conn.close()
        logger.info('IO process is now ready to die, goodbye')


#
# Logic of the main process
#
class SLCAN(AbstractDriver):
    """
    Driver for SLCAN-compatible CAN bus adapters, with extension to support CLI commands.

    Some info on SLCAN can be found here:
        - Linux tree: drivers/net/can/slcan.c (http://lxr.free-electrons.com/source/drivers/net/can/slcan.c)
        - https://files.zubax.com/docs/Generic_SLCAN_API.pdf
        - http://www.can232.com/docs/canusb_manual.pdf
        - http://www.fischl.de/usbtin/

    The CLI extension allows to execute arbitrary CLI commands on the adapter. The commands differ from regular SLCAN
    exchange in the following ways:
        - CLI commands are echoed back.
        - Every output line of a CLI command, including echo, is terminated with CR LF (\r\n).
        - After the last line follows the ASCII End Of Text character (ETX, ^C, ASCII code 0x03) on a separate
          line (terminated with CR LF).
    Example:
        Input command "stat\r\n" may produce the following output lines:
        - Echo: "stat\r\n"
        - Data: "First line\r\n", "Second line\r\n", ...
        - End Of Text marker: "\x03\r\n"
    Refer to https://docs.zubax.com for more info.
    """

    def __init__(self, device_name, **kwargs):
        if not serial:
            raise RuntimeError("PySerial not imported; SLCAN is not available. Please install PySerial.")

        super(SLCAN, self).__init__()

        self._rx_queue = multiprocessing.Queue(maxsize=RX_QUEUE_SIZE)
        self._tx_queue = multiprocessing.Queue(maxsize=TX_QUEUE_SIZE)

        self._cli_command_requests = []     # List of tuples: (command, callback)

        # Removing all unused stuff, because it breaks inter process communications.
        kwargs = copy.copy(kwargs)
        keep_keys = inspect.getargspec(_io_process).args
        for key in list(kwargs.keys()):
            if key not in keep_keys:
                del kwargs[key]

        kwargs['rx_queue'] = self._rx_queue
        kwargs['tx_queue'] = self._tx_queue
        kwargs['parent_pid'] = os.getpid()

        self._proc = multiprocessing.Process(target=_io_process, name='slcan_io_process',
                                             args=(device_name,), kwargs=kwargs)
        self._proc.daemon = True
        self._proc.start()

        deadline = time.monotonic() + IO_PROCESS_INIT_TIMEOUT
        while True:
            try:
                sig = self._rx_queue.get(timeout=IO_PROCESS_INIT_TIMEOUT)
                if sig == IPC_SIGNAL_INIT_OK:
                    break
                if isinstance(sig, Exception):
                    self._tx_queue.put_nowait(IPC_COMMAND_STOP)
                    raise sig
            except queue.Empty:
                pass
            if time.monotonic() > deadline:
                self._tx_queue.put_nowait(IPC_COMMAND_STOP)
                raise DriverError('IO process did not confirm initialization')

        self._check_alive()

    def close(self):
        if self._proc.is_alive():
            self._tx_queue.put(IPC_COMMAND_STOP)
            self._proc.join()

    def __del__(self):
        self.close()

    def _check_alive(self):
        if not self._proc.is_alive():
            raise DriverError('IO process is dead :(')

    def receive(self, timeout=None):
        self._check_alive()

        if timeout is None:
            deadline = None
        elif timeout == 0:
            deadline = 0
        else:
            deadline = time.monotonic() + timeout

        while True:
            # Blockingly reading the queue
            try:
                if deadline is None:
                    get_timeout = None
                elif deadline == 0:
                    # TODO this is a workaround. Zero timeout causes the IPC queue to ALWAYS throw queue.Empty!
                    get_timeout = 1e-3
                else:
                    # TODO this is a workaround. Zero timeout causes the IPC queue to ALWAYS throw queue.Empty!
                    get_timeout = max(1e-3, deadline - time.monotonic())

                obj = self._rx_queue.get(timeout=get_timeout)
            except queue.Empty:
                return

            # Handling the received thing
            if isinstance(obj, CANFrame):
                self._rx_hook(obj)
                return obj

            elif isinstance(obj, Exception):    # Propagating exceptions from the IO process to the main process
                raise obj

            elif isinstance(obj, IPCCommandLineExecutionResponse):
                while len(self._cli_command_requests):
                    (stored_command, stored_callback), self._cli_command_requests = \
                        self._cli_command_requests[0], self._cli_command_requests[1:]
                    if stored_command == obj.command:
                        stored_callback(obj)
                        break

            else:
                raise DriverError('Unexpected entity in IPC channel: %r' % obj)

            # Termination condition
            if deadline == 0:
                break
            elif deadline is not None:
                if time.monotonic() >= deadline:
                    return

    def send(self, message_id, message, extended=False):
        self._check_alive()
        frame = CANFrame(message_id, message, extended)
        try:
            self._tx_queue.put_nowait(frame)
        except queue.Full:
            raise TxQueueFullError()
        self._tx_hook(frame)

    def execute_cli_command(self, command, callback, timeout=None):
        """
        Executes an arbitrary CLI command on the SLCAN adapter, assuming that the adapter supports CLI commands.
        The callback will be invoked from the method receive() using same thread.
        If the command times out, the callback will be invoked anyway, with 'expired' flag set.
        Args:
            command:    Command as unicode string or bytes
            callback:   A callable that accepts one argument.
                        The argument is an instance of IPCCommandLineExecutionResponse
            timeout:    Timeout in seconds. None to use default timeout.
        """
        self._check_alive()
        request = IPCCommandLineExecutionRequest(command, timeout)
        try:
            self._tx_queue.put(request, timeout=timeout)
        except queue.Full:
            raise TxQueueFullError()
        self._cli_command_requests.append((command, callback))
