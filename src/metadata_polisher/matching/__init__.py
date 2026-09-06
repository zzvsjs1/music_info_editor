"""Deterministic matching primitives kept independent of the user interface."""

# Normalisation creates comparison keys while language profiles describe the
# observed script. Release scoring compares album/medium candidates; track
# mapping then determines supported, ordered associations inside a chosen medium.
# Keeping these decisions separate makes their evidence and limitations visible.
#
# Configurable policy owns matching weights and thresholds. A score is evidence
# for the application review layer; matching itself never edits metadata or files.
