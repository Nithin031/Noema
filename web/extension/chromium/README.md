# Noema browser bridge (Brave and Edge)

Sends the active tab's URL and title to your local Noema daemon so browser time gets a real
domain, and shows Noema's nudges inside the page. Manifest V3. Chrome is ignored on purpose.

## Install (once per browser)
1. Brave: open `brave://extensions`. Edge: open `edge://extensions`.
2. Turn on **Developer mode**, click **Load unpacked**, choose this `chromium` folder.
3. Start the daemon (`python -m noema`). The popup shows `registered` / `polling`.

Private windows are never reported. Only `http://127.0.0.1:8765` is contacted.
