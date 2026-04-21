import sys
from pathlib import Path

# Add project root to path when running as installed package
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from cli import cli

if __name__ == "__main__":
    cli()
