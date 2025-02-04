import enum
import os
import subprocess
import threading
from copy import copy
from pathlib import Path

import ipywidgets as ipw
import pexpect
import shortuuid
import traitlets
from aiida import common, orm, plugins, schedulers, transports
from aiida.orm.utils.builders.code import CodeBuilder
from aiida.orm.utils.builders.computer import ComputerBuilder
from aiida.transports.plugins.ssh import parse_sshconfig
from IPython.display import clear_output, display

from .databases import ComputationalResourcesDatabaseWidget
from .utils import StatusHTML

STYLE = {"description_width": "180px"}
LAYOUT = {"width": "400px"}


class ComputationalResourcesWidget(ipw.VBox):
    """Code selection widget.
    Attributes:

    value(Unicode or Code): Trait that points to the selected Code instance.
    It can be set either to an AiiDA Code instance or to a code label (will automatically
    be replaced by the corresponding Code instance). It is linked to the 'value' trait of
    the `self.code_select_dropdown` widget.

    codes(Dict): Trait that contains a dictionary (label => Code instance) for all
    codes found in the AiiDA database for the selected plugin. It is linked
    to the 'options' trait of the `self.code_select_dropdown` widget.

    allow_hidden_codes(Bool): Trait that defines whether to show hidden codes or not.

    allow_disabled_computers(Bool): Trait that defines whether to show codes on disabled
    computers.
    """

    value = traitlets.Union(
        [traitlets.Unicode(), traitlets.Instance(orm.Code)], allow_none=True
    )
    codes = traitlets.Dict(allow_none=True)
    allow_hidden_codes = traitlets.Bool(False)
    allow_disabled_computers = traitlets.Bool(False)
    input_plugin = traitlets.Unicode(allow_none=True)

    def __init__(self, description="Select code:", path_to_root="../", **kwargs):
        """Dropdown for Codes for one input plugin.

        description (str): Description to display before the dropdown.
        """
        self.output = ipw.HTML()
        self.setup_message = StatusHTML()
        self.code_select_dropdown = ipw.Dropdown(
            description=description, disabled=True, value=None
        )
        traitlets.link((self, "codes"), (self.code_select_dropdown, "options"))
        traitlets.link((self.code_select_dropdown, "value"), (self, "value"))

        self.observe(
            self.refresh, names=["allow_disabled_computers", "allow_hidden_codes"]
        )

        self.btn_setup_new_code = ipw.ToggleButton(description="Setup new code")
        self.btn_setup_new_code.observe(self._setup_new_code, "value")

        self._setup_new_code_output = ipw.Output(layout={"width": "500px"})

        children = [
            ipw.HBox([self.code_select_dropdown, self.btn_setup_new_code]),
            self._setup_new_code_output,
            self.output,
        ]
        super().__init__(children=children, **kwargs)

        # Setting up codes and computers.
        self.comp_resources_database = ComputationalResourcesDatabaseWidget(
            input_plugin=self.input_plugin
        )

        self.ssh_computer_setup = SshComputerSetup()
        ipw.dlink(
            (self.ssh_computer_setup, "message"),
            (self.setup_message, "message"),
        )

        ipw.dlink(
            (self.comp_resources_database, "ssh_config"),
            (self.ssh_computer_setup, "ssh_config"),
        )

        self.aiida_computer_setup = AiidaComputerSetup()
        ipw.dlink(
            (self.aiida_computer_setup, "message"),
            (self.setup_message, "message"),
        )
        ipw.dlink(
            (self.comp_resources_database, "computer_setup"),
            (self.aiida_computer_setup, "computer_setup"),
        )

        # Set up AiiDA code.
        self.aiida_code_setup = AiidaCodeSetup()
        ipw.dlink(
            (self.aiida_code_setup, "message"),
            (self.setup_message, "message"),
        )
        ipw.dlink(
            (self.comp_resources_database, "code_setup"),
            (self.aiida_code_setup, "code_setup"),
        )
        self.aiida_code_setup.on_setup_code_success(self.refresh)

        # After a successfull computer setup the codes widget should be refreshed.
        # E.g. the list of available computers needs to be updated.
        self.aiida_computer_setup.on_setup_computer_success(
            self.aiida_code_setup.refresh
        )

        # Quick setup.
        quick_setup_button = ipw.Button(description="Quick Setup")
        quick_setup_button.on_click(self.quick_setup)
        quick_setup = ipw.VBox(
            children=[
                self.ssh_computer_setup.username,
                quick_setup_button,
                self.aiida_code_setup.setup_code_out,
            ]
        )

        # Detailed setup.
        detailed_setup = ipw.Accordion(
            children=[
                self.ssh_computer_setup,
                self.aiida_computer_setup,
                self.aiida_code_setup,
            ]
        )
        detailed_setup.set_title(0, "Set up password-less SSH connection")
        detailed_setup.set_title(1, "Set up a computer in AiiDA")
        detailed_setup.set_title(2, "Set up a code in AiiDA")

        self.output_tab = ipw.Tab(children=[quick_setup, detailed_setup])
        self.output_tab.set_title(0, "Quick Setup")
        self.output_tab.set_title(1, "Detailed Setup")

        self.refresh()

    def quick_setup(self, _=None):
        """Go through all the setup steps automatically."""
        with self.hold_trait_notifications():
            self.ssh_computer_setup._on_setup_ssh_button_pressed()
            if self.aiida_computer_setup.on_setup_computer():
                self.aiida_code_setup.on_setup_code()

    def _get_codes(self):
        """Query the list of available codes."""

        user = orm.User.objects.get_default()

        return {
            self._full_code_label(c[0]): c[0]
            for c in orm.QueryBuilder()
            .append(orm.Code, filters={"attributes.input_plugin": self.input_plugin})
            .all()
            if c[0].computer.is_user_configured(user)
            and (self.allow_hidden_codes or not c[0].hidden)
            and (self.allow_disabled_computers or c[0].computer.is_user_enabled(user))
        }

    @staticmethod
    def _full_code_label(code):
        return f"{code.label}@{code.computer.label}"

    def refresh(self, _=None):
        """Refresh available codes.

        The job of this function is to look in AiiDA database, find available codes and
        put them in the code_select_dropdown widget."""
        self.output.value = ""

        with self.hold_trait_notifications():
            self.code_select_dropdown.options = self._get_codes()
            if not self.code_select_dropdown.options:
                self.output.value = (
                    f"No codes found for input plugin '{self.input_plugin}'."
                )
                self.code_select_dropdown.disabled = True
            else:
                self.code_select_dropdown.disabled = False
            self.code_select_dropdown.value = None

    @traitlets.validate("value")
    def _validate_value(self, change):
        """If code is provided, set it as it is. If code's label is provided,
        select the code and set it."""
        code = change["value"]
        self.output.value = ""

        # If code None, set value to None.
        if code is None:
            return None

        if isinstance(code, str):  # Check code by label.
            if code in self.codes:
                return self.codes[code]
            self.output.value = f"""No code named '<span style="color:red">{code}</span>'
            found in the AiiDA database."""
        elif isinstance(code, orm.Code):  # Check code by value.
            label = self._full_code_label(code)
            if label in self.codes:
                return code
            self.output.value = f"""The code instance '<span style="color:red">{code}</span>'
            supplied was not found in the AiiDA database."""

        # This place will never be reached, because the trait's type is checked.
        return None

    def _setup_new_code(self, _=None):
        with self._setup_new_code_output:
            clear_output()
            if self.btn_setup_new_code.value:
                self._setup_new_code_output.layout = {
                    "width": "500px",
                    "border": "1px solid gray",
                }
                display(
                    ipw.HTML(
                        """Please select the computer/code from a database to pre-fill the fields below."""
                    ),
                    self.comp_resources_database,
                    self.setup_message,
                    self.ssh_computer_setup.password_box,
                    self.output_tab,
                )
            else:
                self._setup_new_code_output.layout = {
                    "width": "500px",
                    "border": "none",
                }


class SshConnectionState(enum.Enum):
    waiting_for_input = -1
    enter_password = 0
    success = 1
    keys_already_present = 2
    do_you_want_to_continue = 3
    no_keys = 4
    unknown_hostname = 5
    connection_refused = 6
    end_of_file = 7


class SshComputerSetup(ipw.VBox):
    """Setup a passwordless access to a computer."""

    ssh_config = traitlets.Dict()
    ssh_connection_state = traitlets.UseEnum(
        SshConnectionState, allow_none=True, default_value=None
    )
    SSH_POSSIBLE_RESPONSES = [
        # order matters! the index will return by pexpect and compare
        # with SshConnectionState
        "[Pp]assword:",  # 0
        "Now try logging into",  # 1
        "All keys were skipped because they already exist on the remote system",  # 2
        "Are you sure you want to continue connecting (yes/no)?",  # 3
        "ERROR: No identities found",  # 4
        "Could not resolve hostname",  # 5
        "Connection refused",  # 6
        pexpect.EOF,  # 7
    ]
    message = traitlets.Unicode()
    password_message = traitlets.Unicode("The passwordless enabling log.")

    def __init__(self, **kwargs):

        self._ssh_connection_message = None
        self._password_message = ipw.HTML()
        ipw.dlink((self, "password_message"), (self._password_message, "value"))
        self._ssh_password = ipw.Password(layout={"width": "150px"}, disabled=True)
        self._continue_with_password_button = ipw.Button(
            description="Continue", layout={"width": "100px"}, disabled=True
        )
        self._continue_with_password_button.on_click(self._send_password)

        self.password_box = ipw.VBox(
            [
                self._password_message,
                ipw.HBox([self._ssh_password, self._continue_with_password_button]),
            ]
        )

        # Username.
        self.username = ipw.Text(
            description="SSH username:", layout=LAYOUT, style=STYLE
        )

        # Port.
        self.port = ipw.IntText(
            description="SSH port:",
            value=22,
            layout=LAYOUT,
            style=STYLE,
        )

        # Hostname.
        self.hostname = ipw.Text(
            description="Computer hostname:",
            layout=LAYOUT,
            style=STYLE,
        )

        # ProxyJump.
        self.proxy_jump = ipw.Text(
            description="ProxyJump:",
            layout=LAYOUT,
            style=STYLE,
        )
        # ProxyJump.
        self.proxy_command = ipw.Text(
            description="ProxyCommand:",
            layout=LAYOUT,
            style=STYLE,
        )

        self._inp_private_key = ipw.FileUpload(
            accept="",
            layout=LAYOUT,
            style=STYLE,
            description="Private key",
            multiple=False,
        )
        self._verification_mode = ipw.Dropdown(
            options=[
                ("Password", "password"),
                ("Use custom private key", "private_key"),
                ("Download public key", "public_key"),
            ],
            layout=LAYOUT,
            style=STYLE,
            value="password",
            description="Verification mode:",
            disabled=False,
        )
        self._verification_mode.observe(
            self._on_verification_mode_change, names="value"
        )
        self._verification_mode_output = ipw.Output()

        self._continue_button = ipw.ToggleButton(
            description="Continue", layout={"width": "100px"}, value=False
        )

        # Setup ssh button and output.
        btn_setup_ssh = ipw.Button(description="Setup ssh")
        btn_setup_ssh.on_click(self._on_setup_ssh_button_pressed)

        children = [
            self.hostname,
            self.port,
            self.username,
            self.proxy_jump,
            self.proxy_command,
            self._verification_mode,
            self._verification_mode_output,
            btn_setup_ssh,
        ]
        super().__init__(children, **kwargs)

    def _ssh_keygen(self):
        """Generate ssh key pair."""
        self.message = "Generating SSH key pair."
        fpath = Path.home() / ".ssh" / "id_rsa"
        keygen_cmd = [
            "ssh-keygen",
            "-f",
            fpath,
            "-t",
            "rsa",
            "-b",
            "4096",
            "-m",
            "PEM",
            "-N",
            "",
        ]
        if not fpath.exists():
            subprocess.run(keygen_cmd, capture_output=True)

    def _can_login(self):
        """Check if it is possible to login into the remote host."""
        # With BatchMode on, no password prompt or other interaction is attempted,
        # so a connect that requires a password will fail.
        ret = subprocess.call(
            [
                "ssh",
                self.hostname.value,
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "true",
            ]
        )
        return ret == 0

    def _is_in_config(self):
        """Check if the config file contains host information."""
        fpath = Path.home() / ".ssh" / "config"
        if not fpath.exists():
            return False
        cfglines = open(fpath).read().split("\n")
        return "Host " + self.hostname.value in cfglines

    def _write_ssh_config(self, private_key_abs_fname=None):
        """Put host information into the config file."""
        fpath = Path.home() / ".ssh" / "config"
        self.message = f"Adding {self.hostname.value} section to {fpath}"
        with open(fpath, "a") as file:
            file.write(f"Host {self.hostname.value}\n")
            file.write(f"  User {self.username.value}\n")
            file.write(f"  Port {self.port.value}\n")
            if self.proxy_jump.value != "":
                file.write(
                    f"  ProxyJump {self.proxy_jump.value.format(username=self.username.value)}\n"
                )
            if self.proxy_command.value != "":
                file.write(
                    f"  ProxyCommand {self.proxy_command.value.format(username=self.username.value)}\n"
                )
            if private_key_abs_fname:
                file.write(f"  IdentityFile {private_key_abs_fname}\n")
            file.write("  ServerAliveInterval 5\n")

    def _on_setup_ssh_button_pressed(self, _=None):
        # Always start by generating a key pair if they are not present.
        self._ssh_keygen()

        # If hostname & username are not provided - do not do anything.
        if self.hostname.value == "":  # check hostname
            self.message = "Please specify the computer hostname."
            return False

        if self.username.value == "":  # check username
            self.message = "Please specify your SSH username."
            return False

        private_key_abs_fname = None
        if self._verification_mode.value == "private_key":
            # unwrap private key file and setting temporary private_key content
            private_key_abs_fname, private_key_content = self._private_key
            if private_key_abs_fname is None:  # check private key file
                self.message = "Please upload your private key file."
                return False

            # Write private key in ~/.ssh/ and use the name of upload file,
            # if exist, generate random string and append to filename then override current name.
            self._add_private_key(private_key_abs_fname, private_key_content)

        if not self._is_in_config():
            self._write_ssh_config(private_key_abs_fname=private_key_abs_fname)

        # Copy public key on the remote computer.
        ssh_connection_thread = threading.Thread(target=self._ssh_copy_id)
        ssh_connection_thread.start()

    def _ssh_copy_id(self):
        """Run the ssh-copy-id command and follow it until it is completed."""
        timeout = 30
        self.password_message = f"Sending public key to {self.hostname.value}... "
        self._ssh_connection_process = pexpect.spawn(
            f"ssh-copy-id {self.hostname.value}"
        )
        while True:
            try:
                idx = self._ssh_connection_process.expect(
                    self.SSH_POSSIBLE_RESPONSES,
                    timeout=timeout,
                )
                self.ssh_connection_state = SshConnectionState(idx)
            except pexpect.TIMEOUT:
                self._ssh_password.disabled = True
                self._continue_with_password_button.disabled = True
                self.password_message = (
                    f"Exceeded {timeout} s timeout. Please start again."
                )
                break

            # Terminating the process when nothing else can be done.
            if self.ssh_connection_state in (
                SshConnectionState.success,
                SshConnectionState.keys_already_present,
                SshConnectionState.no_keys,
                SshConnectionState.unknown_hostname,
                SshConnectionState.connection_refused,
                SshConnectionState.end_of_file,
            ):
                break

        self._ssh_connection_message = None
        self._ssh_connection_process = None

    def _send_password(self, _=None):
        self._ssh_password.disabled = True
        self._continue_with_password_button.disabled = True
        self._ssh_connection_process.sendline(self._ssh_password.value)

    @traitlets.observe("ssh_connection_state")
    def _observe_ssh_connnection_state(self, _=None):
        """Observe the ssh connection state and act according to the changes."""
        if self.ssh_connection_state is SshConnectionState.waiting_for_input:
            return
        if self.ssh_connection_state is SshConnectionState.success:
            self.password_message = (
                "The passwordless connection has been set up successfully."
            )
            return
        if self.ssh_connection_state is SshConnectionState.keys_already_present:
            self.password_message = "The passwordless connection has already been setup. Nothing to be done."
            return
        if self.ssh_connection_state is SshConnectionState.no_keys:
            self.password_message = (
                " Failed\nLooks like the key pair is not present in ~/.ssh folder."
            )
            return
        if self.ssh_connection_state is SshConnectionState.unknown_hostname:
            self.password_message = "Failed\nUnknown hostname."
            return
        if self.ssh_connection_state is SshConnectionState.connection_refused:
            self.password_message = "Failed\nConnection refused."
            return
        if self.ssh_connection_state is SshConnectionState.end_of_file:
            self.password_message = (
                "Didn't manage to connect. Please check your username/password."
            )
            return
        if self.ssh_connection_state is SshConnectionState.enter_password:
            self._handle_ssh_password()
        elif self.ssh_connection_state is SshConnectionState.do_you_want_to_continue:
            self._ssh_connection_process.sendline("yes")

    def _handle_ssh_password(self):
        """Send a password to a remote computer."""
        message = (
            self._ssh_connection_process.before.splitlines()[-1]
            + self._ssh_connection_process.after
        )
        if self._ssh_connection_message == message:
            self._ssh_connection_process.sendline(self._ssh_password.value)
        else:
            self.password_message = (
                f"Please enter {self.username.value}@{self.hostname.value}'s password:"
                if message == b"Password:"
                else f"Please enter {message.decode('utf-8')}"
            )
            self._ssh_password.disabled = False
            self._continue_with_password_button.disabled = False
            self._ssh_connection_message = message

        self.ssh_connection_state = SshConnectionState.waiting_for_input

    def _on_verification_mode_change(self, change):
        """which verification mode is chosen."""
        with self._verification_mode_output:
            clear_output()
            if self._verification_mode.value == "private_key":
                display(self._inp_private_key)
            elif self._verification_mode.value == "public_key":
                public_key = Path.home() / ".ssh" / "id_rsa.pub"
                if public_key.exists():
                    display(
                        ipw.HTML(
                            f"""<pre style="background-color: #253239; color: #cdd3df; line-height: normal; custom=test">{public_key.read_text()}</pre>""",
                            layout={"width": "100%"},
                        )
                    )

    @property
    def _private_key(self):
        """Unwrap private key file and setting filename and file content."""
        if self._inp_private_key.value:
            (fname, _value), *_ = self._inp_private_key.value.items()
            content = copy(_value["content"])
            self._inp_private_key.value.clear()
            self._inp_private_key._counter = 0  # pylint: disable=protected-access
            return fname, content
        return None, None

    @staticmethod
    def _add_private_key(private_key_fname, private_key_content):
        """
        param private_key_fname: string
        param private_key_content: bytes
        """
        fpath = Path.home() / ".ssh" / private_key_fname
        if fpath.exists():
            # if file already exist and has the same content
            if fpath.read_bytes() == private_key_content:
                return fpath.name()

            fpath = fpath / "_" / shortuuid.uuid()
        fpath.write_bytes(private_key_content)

        fpath.chmod(0o600)

        return fpath

    def _reset(self):
        self.hostname.value = ""
        self.port.value = 22
        self.username.value = ""
        self.proxy_jump.value = ""
        self.proxy_command.value = ""

    @traitlets.observe("ssh_config")
    def _observe_ssh_config(self, _=None):
        """Pre-filling the input fields."""
        if not self.ssh_config:
            self._reset()

        if "hostname" in self.ssh_config:
            self.hostname.value = self.ssh_config["hostname"]
        if "port" in self.ssh_config:
            self.port.value = int(self.ssh_config["port"])
        if "proxy_jump" in self.ssh_config:
            self.proxy_jump.value = self.ssh_config["proxy_jump"]
        if "proxy_command" in self.ssh_config:
            self.proxy_command.value = self.ssh_config["proxy_command"]


class AiidaComputerSetup(ipw.VBox):
    """Inform AiiDA about a computer."""

    computer_setup = traitlets.Dict(allow_none=True)
    message = traitlets.Unicode()

    def __init__(self, **kwargs):

        self._on_setup_computer_success = []

        # List of widgets to be displayed.
        self.label = ipw.Text(
            value="",
            placeholder="Will only be used within AiiDA",
            description="Computer name:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Hostname.
        self.hostname = ipw.Text(description="Hostname:", layout=LAYOUT, style=STYLE)

        # Computer description.
        self.description = ipw.Text(
            value="",
            placeholder="No description (yet)",
            description="Computer description:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Directory where to run the simulations.
        self.work_dir = ipw.Text(
            value="/scratch/{username}/aiida_run",
            description="Workdir:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Mpirun command.
        self.mpirun_command = ipw.Text(
            value="mpirun -n {tot_num_mpiprocs}",
            description="Mpirun command:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Number of CPUs per node.
        self.mpiprocs_per_machine = ipw.IntText(
            value=1,
            step=1,
            description="#CPU(s) per node:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Transport type.
        self.transport = ipw.Dropdown(
            value="local",
            options=transports.Transport.get_valid_transports(),
            description="Transport type:",
            style=STYLE,
        )

        # Safe interval.
        self.safe_interval = ipw.FloatText(
            value=30.0,
            description="Min. connection interval (sec):",
            layout=LAYOUT,
            style=STYLE,
        )

        # Scheduler.
        self.scheduler = ipw.Dropdown(
            value="slurm",
            options=schedulers.Scheduler.get_valid_schedulers(),
            description="Scheduler:",
            style=STYLE,
        )

        self.shebang = ipw.Text(
            value="#!/usr/bin/env bash",
            description="Shebang:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Use login shell.
        self.use_login_shell = ipw.Checkbox(value=True, description="Use login shell")

        # Prepend text.
        self.prepend_text = ipw.Textarea(
            placeholder="Text to prepend to each command execution",
            description="Prepend text:",
            layout=LAYOUT,
        )

        # Append text.
        self.append_text = ipw.Textarea(
            placeholder="Text to append to each command execution",
            description="Append text:",
            layout=LAYOUT,
        )

        # Buttons and outputs.
        self.setup_button = ipw.Button(description="Setup computer")
        self.setup_button.on_click(self.on_setup_computer)
        test_button = ipw.Button(description="Test computer")
        test_button.on_click(self.test)
        self._test_out = ipw.Output(layout=LAYOUT)

        # Organize the widgets
        children = [
            self.label,
            self.hostname,
            self.description,
            self.work_dir,
            self.mpirun_command,
            self.mpiprocs_per_machine,
            self.transport,
            self.safe_interval,
            self.scheduler,
            self.shebang,
            self.use_login_shell,
            self.prepend_text,
            self.append_text,
            self.setup_button,
            test_button,
            self._test_out,
        ]

        super().__init__(children, **kwargs)

    def _configure_computer(self, computer):
        """Configure the computer"""
        sshcfg = parse_sshconfig(self.hostname.value)
        authparams = {
            "port": int(sshcfg.get("port", 22)),
            "look_for_keys": True,
            "key_filename": os.path.expanduser(
                sshcfg.get("identityfile", ["~/.ssh/id_rsa"])[0]
            ),
            "timeout": 60,
            "allow_agent": True,
            "proxy_jump": "",
            "proxy_command": "",
            "compress": True,
            "gss_auth": False,
            "gss_kex": False,
            "gss_deleg_creds": False,
            "gss_host": self.hostname.value,
            "load_system_host_keys": True,
            "key_policy": "WarningPolicy",
            "use_login_shell": self.use_login_shell.value,
            "safe_interval": self.safe_interval.value,
        }
        if "user" in sshcfg:
            authparams["username"] = sshcfg["user"]
        else:
            print(
                f"SSH username is not provided, please run `verdi computer configure {self.label.value}` "
                "from the command line."
            )
            return False
        if "proxycommand" in sshcfg:
            authparams["proxy_command"] = sshcfg["proxycommand"]
        elif "proxyjump" in sshcfg:
            authparams["proxy_jump"] = sshcfg["proxyjump"]

        computer.configure(**authparams)
        return True

    def on_setup_computer(self, _=None):
        """Create a new computer."""
        if self.label.value == "":  # check hostname
            self.message = "Please specify the computer name (for AiiDA)"
            return False
        try:
            computer = orm.Computer.objects.get(label=self.label.value)
            self.message = f"A computer called {self.label.value} already exists."
            for function in self._on_setup_computer_success:
                function()
            return True
        except common.NotExistent:
            pass

        items_to_configure = [
            "label",
            "hostname",
            "description",
            "work_dir",
            "mpirun_command",
            "mpiprocs_per_machine",
            "transport",
            "scheduler",
            "prepend_text",
            "append_text",
            "shebang",
        ]
        kwargs = {key: getattr(self, key).value for key in items_to_configure}

        computer_builder = ComputerBuilder(**kwargs)
        try:
            computer = computer_builder.new()
        except (
            ComputerBuilder.ComputerValidationError,
            common.exceptions.ValidationError,
        ) as err:
            self.message = f"{type(err).__name__}: {err}"
            return False

        try:
            computer.store()
        except common.exceptions.ValidationError as err:
            self.message = f"Unable to store the computer: {err}."
            return False

        if self._configure_computer(computer):
            for function in self._on_setup_computer_success:
                function()
        self.message = f"Computer<{computer.pk}> {computer.label} created"
        return True

    def on_setup_computer_success(self, function):
        self._on_setup_computer_success.append(function)

    def test(self, _=None):
        with self._test_out:
            clear_output()
            print(
                subprocess.check_output(
                    ["verdi", "computer", "test", "--print-traceback", self.label.value]
                ).decode("utf-8")
            )

    def _reset(self):
        self.label.value = ""
        self.hostname.value = ""
        self.description.value = ""
        self.work_dir.value = ""
        self.mpirun_command.value = "mpirun -n {tot_num_mpiprocs}"
        self.mpiprocs_per_machine.value = 1
        self.transport.value = "ssh"
        self.safe_interval.value = 30.0
        self.scheduler.value = "slurm"
        self.shebang.value = "#!/usr/bin/env bash"
        self.use_login_shell.value = True
        self.prepend_text.value = ""
        self.append_text.value = ""

    @traitlets.observe("computer_setup")
    def _observe_computer_setup(self, _=None):
        # Setup.
        if not self.computer_setup:
            self._reset()
            return
        if "setup" in self.computer_setup:
            for key, value in self.computer_setup["setup"].items():
                if hasattr(self, key):
                    getattr(self, key).value = value
        # Configure.
        if "configure" in self.computer_setup:
            for key, value in self.computer_setup["configure"].items():
                if hasattr(self, key):
                    getattr(self, key).value = value


class AiidaCodeSetup(ipw.VBox):
    """Class that allows to setup AiiDA code"""

    code_setup = traitlets.Dict(allow_none=True)
    message = traitlets.Unicode()

    def __init__(self, path_to_root="../", **kwargs):

        self._on_setup_code_success = []

        # Code label.
        self.label = ipw.Text(
            description="AiiDA code label:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Computer on which the code is installed. Two dlinks are needed to make sure we get a Computer instance.
        self.computer = ComputerDropdownWidget(
            path_to_root=path_to_root,
        )

        # Code plugin.
        self.input_plugin = ipw.Dropdown(
            options=sorted(
                (ep.name, ep)
                for ep in plugins.entry_point.get_entry_points("aiida.calculations")
            ),
            description="Code plugin:",
            layout=LAYOUT,
            style=STYLE,
        )

        # Code description.
        self.description = ipw.Text(
            placeholder="No description (yet)",
            description="Code description:",
            layout=LAYOUT,
            style=STYLE,
        )

        self.remote_abs_path = ipw.Text(
            placeholder="/path/to/executable",
            description="Absolute path to executable:",
            layout=LAYOUT,
            style=STYLE,
        )

        self.prepend_text = ipw.Textarea(
            placeholder="Text to prepend to each command execution",
            description="Prepend text:",
            layout=LAYOUT,
        )

        self.append_text = ipw.Textarea(
            placeholder="Text to append to each command execution",
            description="Append text:",
            layout=LAYOUT,
        )

        btn_setup_code = ipw.Button(description="Setup code")
        btn_setup_code.on_click(self.on_setup_code)
        self.setup_code_out = ipw.Output()

        children = [
            self.label,
            self.computer,
            self.input_plugin,
            self.description,
            self.remote_abs_path,
            self.prepend_text,
            self.append_text,
            btn_setup_code,
            self.setup_code_out,
        ]
        super().__init__(children, **kwargs)

    @traitlets.validate("input_plugin")
    def _validate_input_plugin(self, proposal):
        plugin = proposal["value"]
        return plugin if plugin in self.input_plugin.options else None

    def on_setup_code(self, _=None):
        """Setup an AiiDA code."""
        with self.setup_code_out:
            clear_output()

            if not self.computer.value:
                self.message = "Please select an existing computer."
                return False

            items_to_configure = [
                "label",
                "computer",
                "description",
                "input_plugin",
                "remote_abs_path",
                "prepend_text",
                "append_text",
            ]

            kwargs = {key: getattr(self, key).value for key in items_to_configure}
            kwargs["code_type"] = CodeBuilder.CodeType.ON_COMPUTER

            # Checking if the code with this name already exists
            qb = orm.QueryBuilder()
            qb.append(
                orm.Computer, filters={"uuid": kwargs["computer"].uuid}, tag="computer"
            )
            qb.append(
                orm.Code, with_computer="computer", filters={"label": kwargs["label"]}
            )
            if qb.count() > 0:
                self.message = (
                    f"Code {kwargs['label']}@{kwargs['computer'].label} already exists."
                )
                return False

            try:
                code = CodeBuilder(**kwargs).new()
            except (common.exceptions.InputValidationError, KeyError) as exception:
                self.message = f"Invalid inputs: {exception}"
                return False

            try:
                code.store()
                code.reveal()
            except common.exceptions.ValidationError as exception:
                self.message = f"Unable to store the Code: {exception}"
                return False

            for function in self._on_setup_code_success:
                function()

            self.message = f"Code<{code.pk}> {code.full_label} created"

            return True

    def on_setup_code_success(self, function):
        self._on_setup_code_success.append(function)

    def _reset(self):
        self.label.value = ""
        self.computer.value = ""
        self.description.value = ""
        self.remote_abs_path.value = ""
        self.prepend_text.value = ""
        self.append_text.value = ""

    def refresh(self):
        self.computer.refresh()
        self._observe_code_setup()

    @traitlets.observe("code_setup")
    def _observe_code_setup(self, _=None):
        # Setup.
        if not self.code_setup:
            self._reset()
        for key, value in self.code_setup.items():
            if hasattr(self, key):
                if key == "input_plugin":
                    getattr(self, key).label = value
                else:
                    getattr(self, key).value = value


class ComputerDropdownWidget(ipw.VBox):
    """Widget to select a configured computer.

    Attributes:
        selected_computer(Unicode or Computer): Trait that points to the selected Computer instance.
            It can be set either to an AiiDA Computer instance or to a computer label (will
            automatically be replaced by the corresponding Computer instance). It is linked to the
            'value' trait of `self._dropdown` widget.

        computers(Dict): Trait that contains a dictionary (label => Computer instance) for all
        computers found in the AiiDA database. It is linked to the 'options' trait of
        `self._dropdown` widget.

        allow_select_disabled(Bool):  Trait that defines whether to show disabled computers.
    """

    value = traitlets.Union(
        [traitlets.Unicode(), traitlets.Instance(orm.Computer)], allow_none=True
    )
    computers = traitlets.Dict(allow_none=True)
    allow_select_disabled = traitlets.Bool(False)

    def __init__(self, description="Select computer:", path_to_root="../", **kwargs):
        """Dropdown for configured AiiDA Computers.

        description (str): Text to display before dropdown.

        path_to_root (str): Path to the app's root folder.
        """

        self.output = ipw.HTML()
        self._dropdown = ipw.Dropdown(
            options={},
            value=None,
            description=description,
            style=STYLE,
            layout=LAYOUT,
            disabled=True,
        )
        traitlets.link((self, "computers"), (self._dropdown, "options"))
        traitlets.link((self._dropdown, "value"), (self, "value"))

        self.observe(self.refresh, names="allow_select_disabled")

        children = [
            ipw.HBox(
                [
                    self._dropdown,
                ]
            ),
            self.output,
        ]
        self.refresh()
        super().__init__(children=children, **kwargs)

    def _get_computers(self):
        """Get the list of available computers."""

        # Getting the current user.
        user = orm.User.objects.get_default()

        return {
            c[0].label: c[0]
            for c in orm.QueryBuilder().append(orm.Computer).all()
            if c[0].is_user_configured(user)
            and (self.allow_select_disabled or c[0].is_user_enabled(user))
        }

    def refresh(self, _=None):
        """Refresh the list of configured computers."""
        self.output.value = ""
        with self.hold_trait_notifications():
            self._dropdown.options = self._get_computers()
            if not self.computers:
                self.output.value = "No computers found."
                self._dropdown.disabled = True
            else:
                self._dropdown.disabled = False

            self._dropdown.value = None

    @traitlets.validate("value")
    def _validate_value(self, change):
        """Select computer either by label or by class instance."""
        computer = change["value"]
        self.output.value = ""
        if not computer:
            return None
        if isinstance(computer, str):
            if computer in self.computers:
                return self.computers[computer]
            self.output.value = f"""Computer instance '<span style="color:red">{computer}</span>'
            is not configured in your AiiDA profile."""
            return None

        if isinstance(computer, orm.Computer):
            if computer.label in self.computers:
                return computer
            self.output.value = f"""Computer instance '<span style="color:red">{computer.label}</span>'
            is not configured in your AiiDA profile."""
        return None
