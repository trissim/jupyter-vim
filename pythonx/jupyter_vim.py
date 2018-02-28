#=============================================================================
#    File: pythonx/jupyter_vim.py
# Created: 07/28/11 22:14:58
#  Author: Paul Ivanov (http://pirsquared.org)
#  Updated: [11/13/2017] William Van Vliet
#  Updated: [02/14/2018, 12:31] Bernie Roesler
#
# Description:
"""
Python code for ftplugin/python/jupyter.vim.
"""
#=============================================================================

from __future__ import print_function
import os
import re
import signal
import sys

import textwrap
from queue import Empty

is_py3 = sys.version_info[0] >= 3
if is_py3:
    unicode = str

# 'vim' can only be imported when running on the vim interpreter. Create NoOp
# class to allow testing of functions outside of vim (sortof... a lot break)
not_in_vim = False
try:
    import vim
except ImportError:
    class NoOp(object):
        """Don't do anything."""
        def __getattribute__(self, key):
            return lambda *args: '0'
    vim = NoOp()
    not_in_vim = True
    print("Uh oh! Not running inside vim! Loading anyway...")

#------------------------------------------------------------------------------
#        Define wrapper for encoding
#------------------------------------------------------------------------------
# get around unicode problems when interfacing with vim
vim_encoding = vim.eval('&encoding') or 'utf-8'

def vim2pystr(var):
    # Convert to proper encoding
    if is_py3 and isinstance(var, bytes):
        var = str(var, vim_encoding)
    elif not is_py3 and isinstance(var, str):
        var = unicode(var, vim_encoding)
    return var

#------------------------------------------------------------------------------
#        Read global configuration variables
#------------------------------------------------------------------------------
monitor_subchannel = bool(int(vim.vars.get("g:ipy_monitor_subchannel", '0')))
current_stdin_prompt = {}

prompt_in = 'In [%(line)]: '
prompt_out = 'Out[%(line)]: '

_install_instructions = """You *must* install IPython into the Python that
your vim is linked against. If you are seeing this message, this usually means
either (1) installing IPython using the system Python that vim is using, or
(2) recompiling Vim against the Python where you already have IPython
installed. This is only a requirement to allow Vim to speak with an IPython
instance using IPython's own machinery. It does *not* mean that the IPython
instance with which you communicate via vim-ipython needs to be running the
same version of Python.
"""

#------------------------------------------------------------------------------
#        Check Connection:
#------------------------------------------------------------------------------
def check_connection():
    """Check that we have a client connected to the kernel."""
    kc.hb_channel.unpause()
    return kc.hb_channel.is_beating()

def disconnect():
    """Disconnect kernel client."""
    kc.stop_channels()

# if module has not yet been imported, define global kernel manager, client and
# kernel pid. Otherwise, just check that we're connected to a kernel.
if all([x in locals() and x in globals() for x in ['km', 'kc', 'pid']]):
    check_connection()
else:
    km = None
    kc = None
    pid = None
    send = None

#------------------------------------------------------------------------------
#        Function Definitions:
#------------------------------------------------------------------------------
def connect_to_kernel():
    """ Create kernel manager from existing connection file """
    try:
        import IPython
    except ImportError:
        raise ImportError("Could not find kernel. " + _install_instructions)

    from jupyter_client import KernelManager, find_connection_file

    global kc, km, pid, send

    # Test if connection is alive
    connected = False
    attempt = 0
    max_attempts = 5
    while not connected and attempt < max_attempts:
        attempt += 1
        try:
            # Default: filename='kernel-*.json'
            cfile = find_connection_file()
        except IOError:
            vim_echom("kernel connection attempt #{:d} failed - no kernel file"\
                    .format(attempt), "Error")
            continue

        # Create the kernel manager and connect a client
        km = KernelManager(connection_file=cfile)
        km.load_connection_file()
        kc = km.client()
        kc.start_channels()

        # Alias execute function
        def _send(msg, **kwargs):
            """Send a message to the kernel client."""
            # Include dedent of msg so we don't get odd indentation errors.
            return kc.execute(textwrap.dedent(msg), **kwargs)
        send = _send

        # Ping the kernel
        send('', silent=True)
        try:
            reply = kc.get_shell_msg(timeout=1)
        except Empty:
            continue
        else:
            connected = True
            # Send command so that monitor knows vim is commected
            # send('"_vim_client"', store_history=False)
            pid = set_pid() # Ask kernel for its PID
            vim.command('redraw')
            vim_echom("kernel connection successful! pid = {}".format(pid),
                      style="Operator")
        finally:
            if not connected:
                kc.stop_channels()
                vim_echom("kernel connection attempt timed out", "Error")

def vim_echom(arg, style="Question"):
    """ Report arg using vim's echomessage command.

    Keyword args:
    style -- the vim highlighting style to use
    """
    try:
        vim.command("echohl %s" % style)
        vim.command("echom \"%s\"" % arg.replace('\"', '\\\"'))
        vim.command("echohl None")
    except vim.error:
        print("-- %s" % arg)

# from <http://serverfault.com/questions/71285/\
# in-centos-4-4-how-can-i-strip-escape-sequences-from-a-text-file>
strip = re.compile(r'\x1B\[([0-9]{1,2}(;[0-9]{1,2})*)?[mK]')
def strip_color_escapes(s):
    """Remove ANSI color escape sequences from a string."""
    return strip.sub('', s)

def update_subchannel_msgs(force=False):
    """Grab any pending messages and place them inside the vim-ipython shell.
    This function will do nothing if the vim-ipython shell is not visible,
    unless force=True argument is passed.
    """
    if not force:
        return False

    # Save which window we're in
    cur_win = vim.eval('win_getid()')

    # Open the ipython terminal in vim, and move cursor to it
    is_console_open = vim.eval('jupyter#OpenJupyterTerm()')
    if not is_console_open:
        vim_echom('__jupyter_term__ failed to open!', 'Error')
        return False

    #--------------------------------------------------------------------------
    #        Message handler
    #--------------------------------------------------------------------------
    global current_stdin_prompt
    msgs = kc.iopub_channel.get_msgs()
    msgs += kc.stdin_channel.get_msgs() # get prompts from kernel
    b = vim.current.buffer
    update_occured = False
    for m in msgs:
        # if we received a message it means the kernel is not waiting for input
        # vim.command('autocmd! InsertEnter <buffer>')
        current_stdin_prompt.clear()
        s = ''

        if 'msg_type' not in m['header']:
            continue

        msg_type = m['header']['msg_type']

        if msg_type == 'status':
            continue
        elif msg_type == 'stream':
            # TODO: alllow for distinguishing between stdout and stderr (using
            # custom syntax markers in the vim-ipython buffer perhaps), or by
            # also echoing the message to the status bar
            s = strip_color_escapes(m['content']['text'])
        elif msg_type == 'pyout' or msg_type == 'execute_result':
            s = prompt_out % {'line': m['content']['execution_count']}
            s += m['content']['data']['text/plain']
        elif msg_type == 'display_data':
            # TODO: handle other display data types (HMTL? images?)
            s += m['content']['data']['text/plain']
        elif msg_type == 'pyin' or msg_type == 'execute_input':
            # TODO: the next line allows us to resend a line to ipython if
            # %doctest_mode is on. In the future, IPython will send the
            # execution_count on subchannel, so this will need to be updated
            # once that happens
            line_number = m['content'].get('execution_count', 0)
            prompt = prompt_in % {'line': line_number}
            s = prompt
            # add a continuation line (with trailing spaces if the prompt has them)
            dots = '.' * len(prompt.rstrip())
            dots += prompt[len(prompt.rstrip()):]
            s += m['content']['code'].rstrip().replace('\n', '\n' + dots)
        elif msg_type == 'pyerr' or msg_type == 'error':
            c = m['content']
            s = "\n".join(map(strip_color_escapes, c['traceback']))
        elif msg_type == 'input_request':
            vim_echom('python input not supported in vim.', 'Error')
            return False
            # current_stdin_prompt['prompt'] = m['content']['prompt']
            # current_stdin_prompt['is_password'] = m['content']['password']
            # current_stdin_prompt['parent_msg_id'] = m['parent_header']['msg_id']
            # s += m['content']['prompt']
            # vim_echom('Awaiting input. call :IPythonInput to reply')

        if s.find('\n') == -1:
            # somewhat ugly unicode workaround from
            # http://vim.1045645.n5.nabble.com/Limitations-of-vim-python-interface-with-respect-to-character-encodings-td1223881.html
            if isinstance(s, unicode):
                s = s.encode(vim_encoding)
            b.append(s)
        else:
            try:
                b.append(s.splitlines())
            except:
                b.append([l.encode(vim_encoding) for l in s.splitlines()])
        update_occured = True

    if update_occured or force:
        vim.command('normal! G') # go to the end of the file
        if current_stdin_prompt:
            vim.command('normal! $') # also go to the end of the line

    # Move cursor back to original window
    vim.command(':call win_gotoid({})'.format(cur_win))

    return update_occured

def get_reply_msg(msg_id):
    """Get kernel reply from sent client message with msg_id."""
    while True:
        try:
            m = kc.get_shell_msg(timeout=1)
        except Empty:
            continue
        if m['parent_header']['msg_id'] == msg_id:
            return m

def print_prompt(prompt, msg_id=None):
    """Print In[] or In[56] style messages on Vim's display line."""
    if msg_id:
        # wait to get message back from kernel
        try:
            reply = get_reply_msg(msg_id)
            count = reply['content']['execution_count']
            vim_echom("In[%d]: %s" % (count, prompt))
        except Empty:
            # if the kernel is waiting for input it's normal to get no reply
            if not kc.stdin_channel.msg_ready():
                vim_echom("In[]: %s (no reply from IPython kernel)" % prompt)
    else:
        vim_echom("In[]: %s" % prompt)

def with_subchannel(f, *args, **kwargs):
    """Conditionally monitor subchannel."""
    def f_with_update(*args, **kwargs):
        if not check_connection():
            vim_echom('WARNING: Not connected to IPython!', 'WarningMsg')
            return
        try:
            f(*args, **kwargs)
            if monitor_subchannel:
                update_subchannel_msgs(force=True)
        except AttributeError: #if kc is None
            vim_echom("not connected to IPython", 'Error')
    return f_with_update

@with_subchannel
def run_file(flags='', filename=''):
    """Run a given python file using ipython's %run magic."""
    ext = os.path.splitext(filename)[-1][1:]
    if ext in ('pxd', 'pxi', 'pyx', 'pyxbld'):
        cmd = ' '.join(filter(None, (
            '%run_cython',
            vim2pystr(vim.vars.get('cython_run_flags', '')),
            repr(filename))))
    else:
        b = vim.current.buffer
        cmd = '%run {} {}'.format((flags or vim2pystr(b.vars['ipython_run_flags'])),
                                  repr(filename))
    msg_id = send(cmd)
    # print_prompt(cmd, msg_id)

@with_subchannel
def run_command(cmd):
    """Send a single command to the kernel."""
    msg_id = send(cmd)
    # print_prompt(cmd, msg_id)

@with_subchannel
def send_range():
    """Send a range of lines from the current vim buffer to the kernel."""
    r = vim.current.range
    print("range = {},{}".format(r.start, r.end))
    lines = "\n".join(vim.current.buffer[r.start:r.end+1])
    msg_id = send(lines)
    # prompt = "lines %d-%d "% (r.start+1,r.end+1)
    # print_prompt(prompt,msg_id)

def set_pid():
    """Explicitly ask the ipython kernel for its pid."""
    the_pid = -1
    code = 'import os; _pid = os.getpid()'
    msg_id = send(code, silent=True, user_expressions={'_pid':'_pid'})

    # wait to get message back from kernel
    try:
        reply = get_reply_msg(msg_id)
    except Empty:
        vim_echom("no reply from IPython kernel", "WarningMsg")
        return -1

    try:
        the_pid = int(reply['content']['user_expressions']\
                        ['_pid']['data']['text/plain'])
    except KeyError:
        vim_echom("Could not get PID information, kernel not running Python?")

    return the_pid

def terminate_kernel_hack():
    """Send SIGTERM to the IPython kernel."""
    interrupt_kernel_hack(signal.SIGTERM)

def interrupt_kernel_hack(signal_to_send=None):
    """
    Sends the interrupt signal to the remote kernel. This side steps the
    (non-functional) ipython interrupt mechanisms.
    Only works on posix.
    """
    if pid is None:
        vim_echom("cannot get kernel PID, Ctrl-C will not be supported")
        return

    if signal_to_send is None:
        signal_to_send = signal.SIGINT

    try:
        os.kill(pid, int(signal_to_send))
        vim_echom("KeyboardInterrupt (sent to ipython: pid " +
                  "%i with signal %s)" % (pid, signal_to_send), "WarningMsg")
    except OSError:
        vim_echom("unable to kill pid %d" % pid)

def is_cell_separator(line):
    """Determines whether a given line is a cell separator"""
    cell_sep = ['##', '#%%%%', '# <codecell>']
    for sep in cell_sep:
        if line.strip().startswith(sep):
            return True
    return False

@with_subchannel
def run_this_cell():
    """Runs all the code in between two cell separators"""
    cur_buf = vim.current.buffer
    (cur_line, cur_col) = vim.current.window.cursor
    cur_line -= 1

    # Search upwards for cell separator
    upper_bound = cur_line
    while upper_bound > 0 and not is_cell_separator(cur_buf[upper_bound]):
        upper_bound -= 1

    # Skip past the first cell separator if it exists
    if is_cell_separator(cur_buf[upper_bound]):
        upper_bound += 1

    # Search downwards for cell separator
    lower_bound = min(upper_bound+1, len(cur_buf)-1)

    while lower_bound < len(cur_buf)-1 and not is_cell_separator(cur_buf[lower_bound]):
        lower_bound += 1

    # Move before the last cell separator if it exists
    if is_cell_separator(cur_buf[lower_bound]):
        lower_bound -= 1

    # Make sure bounds are within buffer limits
    upper_bound = max(0, min(upper_bound, len(cur_buf)-1))
    lower_bound = max(0, min(lower_bound, len(cur_buf)-1))

    # Make sure of proper ordering of bounds
    lower_bound = max(upper_bound, lower_bound)

    # Calculate minimum indentation level of entire cell
    shiftwidth = vim.eval('&shiftwidth')
    count = lambda x: int(vim.eval('indent(%d)/%s' % (x, shiftwidth)))

    min_indent = count(upper_bound+1)
    for i in range(upper_bound+1, lower_bound):
        indent = count(i)
        if indent < min_indent:
            min_indent = indent

    # Perform dedent
    if min_indent > 0:
        vim.command('%d,%d%s' % (upper_bound+1, lower_bound+1, '<'*min_indent))

    # Execute cell
    lines = "\n".join(cur_buf[upper_bound:lower_bound+1])
    msg_id = send(lines)
    prompt = "lines %d-%d "% (upper_bound+1, lower_bound+1)
    print_prompt(prompt, msg_id)

    # Re-indent
    if min_indent > 0:
        vim.command("silent undo")

#def set_breakpoint():
#    send("__IP.InteractiveTB.pdb.set_break('%s',%d)" % (vim.current.buffer.name,
#                                                        vim.current.window.cursor[0]))
#    print("set breakpoint in %s:%d"% (vim.current.buffer.name,
#                                      vim.current.window.cursor[0]))
#
#def clear_breakpoint():
#    send("__IP.InteractiveTB.pdb.clear_break('%s',%d)" % (vim.current.buffer.name,
#                                                          vim.current.window.cursor[0]))
#    print("clearing breakpoint in %s:%d" % (vim.current.buffer.name,
#                                            vim.current.window.cursor[0]))
#
#def clear_all_breakpoints():
#    send("__IP.InteractiveTB.pdb.clear_all_breaks()");
#    print("clearing all breakpoints")
#
#def run_this_file_pdb():
#    send(' __IP.InteractiveTB.pdb.run(\'execfile("%s")\')' % (vim.current.buffer.name,))
#    #send('run -d %s' % (vim.current.buffer.name,))
#    echo("In[]: run -d %s (using pdb)" % vim.current.buffer.name)

if not_in_vim:
    print('done.')

#==============================================================================
#==============================================================================
