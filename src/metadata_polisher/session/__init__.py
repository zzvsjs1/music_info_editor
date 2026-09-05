"""Immutable in-memory session state and UI-thread result reducers."""

# Session transforms replace complete values on the UI thread. Worker results
# carry captured revisions so reducers can reject evidence from an older state.
