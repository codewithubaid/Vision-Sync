#!/usr/bin/env python3
"""Pretend NetworkManager for the tests. State lives in $FAKE_NM (JSON)."""
import json, os, sys
path = os.environ["FAKE_NM"]
st = json.load(open(path))
a = sys.argv[1:]
st.setdefault("calls", []).append(a)
def save(): json.dump(st, open(path, "w"))
def esc(s): return s.replace("\\", "\\\\").replace(":", "\\:")
out, rc, err = "", 0, ""
if a[:4] == ["-t", "-f", "DEVICE,TYPE", "device"]:
    out = "wlan0:wifi\neth0:ethernet\nlo:loopback\n"
elif a[:6] == ["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"]:
    out = "".join("%s:%s:%s\n" % (esc(n), t, d) for n, t, d in st["active"])
elif a[:6] == ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]:
    if any(n == "VisionSync-Setup" for n, _, _ in st["active"]):
        rc, err = 10, "Error: scanning not allowed while hosting"
    else:
        out = "".join("%s:%d:%s\n" % (esc(s), sig, sec) for s, sig, sec in st["networks"])
elif a[:2] == ["connection", "delete"]:
    rc = 0
elif a[:2] == ["connection", "add"]:
    st["ap_psk"] = a[a.index("wifi-sec.psk") + 1]
    st["ap_ssid"] = a[a.index("ssid") + 1]
    rc = 1 if st.get("ap_fails") else 0
elif a[:2] == ["connection", "up"]:
    st["active"].append([a[2], "802-11-wireless", "wlan0"])
elif a[:2] == ["connection", "down"]:
    st["active"] = [r for r in st["active"] if r[0] != a[2]]
elif a[:2] == ["radio", "wifi"]:
    st["radio"] = a[2]
    if a[2] == "off":
        st["active"] = [r for r in st["active"] if r[1] != "802-11-wireless"]
elif "connect" in a and "wifi" in a:
    ssid = a[a.index("connect") + 1]
    pw = a[a.index("password") + 1] if "password" in a else ""
    if st["known"].get(ssid) == pw:
        st["active"].append([ssid, "802-11-wireless", "wlan0"])
    else:
        rc, err = 4, "Error: Connection activation failed: Secrets were required, but not provided."
else:
    rc, err = 2, "fake nmcli: unknown " + " ".join(a)
save()
sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
