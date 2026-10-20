from wisp.cli.app import WispApp
from wisp.config.settings import ensure_user_config


def main() -> None:
    ensure_user_config()
    app = WispApp()
    app.run()


if __name__ == "__main__":
    main()
