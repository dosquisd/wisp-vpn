[Unit]
Description=Privileged daemon for Wisp
Requires=wisp.socket

[Service]
Type=simple
ExecStart={python_executable} -m wisp.daemon.server
User=root
NonBlocking=true

[Install]
WantedBy=multi-user.target
