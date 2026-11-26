"""Optional password gate for deployed copies. Set APP_PASSWORD in the .env file, in the environment, or in Streamlit
secrets. Without it the app is open to anyone who can reach the page, which is fine on your own PC but not when deployed."""
import hmac
import os
import time
from pathlib import Path

import streamlit as st


def _password():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        f = Path(__file__).resolve().parent / ".env"
        if f.exists():
            for ln in f.read_text(encoding="utf-8").splitlines():
                if ln.strip().startswith("APP_PASSWORD="):
                    pw = ln.split("=", 1)[1].strip().strip('"').strip("'")
    if not pw:
        try:
            pw = st.secrets.get("APP_PASSWORD")  # raises if there is no secrets file
        except Exception:
            pw = None
    return pw or None


def configured():
    return _password() is not None


def gate():
    """Stops the script with a sign-in form until the right password is entered (no-op when no password is set)."""
    pw = _password()
    if pw is None or st.session_state.get("authed"):
        return
    st.title("Sign in")
    tries = st.session_state.get("tries", 0)
    if tries >= 5:
        st.error("Too many wrong attempts. Reload the page to try again.")
        st.stop()
    entered = st.text_input("Password", type="password", key="auth_pw")
    if entered:
        if hmac.compare_digest(entered.encode(), pw.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.session_state["tries"] = tries + 1
        time.sleep(1.5)  # slow down guessing
        st.error("Wrong password.")
    st.stop()
