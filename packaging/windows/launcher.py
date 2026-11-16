"""The Windows program's entry: PyInstaller builds RoutinePanel.exe from this (see build.ps1)."""
import sys

from routine_panel.app import main

sys.exit(main(sys.argv[1:]))
