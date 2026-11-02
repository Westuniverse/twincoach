# SPDX-License-Identifier: GPL-2.0-only
"""Import the selected Full33 entry inside the fresh spawn interpreter."""


def run_full33_preview(*args, entry_directory):
    import posepriv_coach
    posepriv_coach.__path__.insert(0, entry_directory)
    from posepriv_coach.field_preview_full33_split import _run
    return _run(*args)
