# Contributing to bigprint

Thanks for helping! Bug reports, print photos, ideas and code are all welcome.

## Reporting a bug

Open an issue using the **Bug report** template. The most useful things to
include:

- what you did (settings or the command you ran) and what happened;
- the bigprint version (`bigprint --version`, or the name of the zip you downloaded);
- the log: the **Log** section in the GUI, or `%LOCALAPPDATA%\bigprint\bigprint-gui.log`
  for the Windows app;
- if possible, the model or a link to it. Please only share models you have the
  right to share.

## Development setup

```
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate on macOS/Linux
pip install -e .[dev]
pytest
```

All tests should pass before you open a pull request. GitHub runs them
automatically on Windows, macOS and Linux.

Run the GUI from source with `bigprint-gui`, and the command line with
`bigprint`.

## Pull requests

- Keep each pull request focused on one change, and describe what it changes
  and why.
- Add or update tests for behaviour changes; bug fixes should come with a test
  that would have caught the bug.
- Match the style of the surrounding code.
- New source files start with the license header:

  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  ```

## License of contributions

bigprint is licensed under the GNU Affero General Public License v3.0 or later.
By submitting a contribution, you agree that it is licensed under the same
license, and that you have the right to submit it.
