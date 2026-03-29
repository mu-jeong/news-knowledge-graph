from typing import Any, Dict

import streamlit as st


ANALYSIS_SCOPE_KEY = "analysis_scope"


def ensure_analysis_scope() -> Dict[str, Any]:
    if ANALYSIS_SCOPE_KEY not in st.session_state:
        st.session_state[ANALYSIS_SCOPE_KEY] = {
            "keyword": "",
            "date_from": None,
            "date_to": None,
        }
    return st.session_state[ANALYSIS_SCOPE_KEY]


def get_analysis_scope() -> Dict[str, Any]:
    return ensure_analysis_scope()


def set_analysis_scope(keyword: str, date_from=None, date_to=None) -> Dict[str, Any]:
    scope = {
        "keyword": (keyword or "").strip(),
        "date_from": date_from,
        "date_to": date_to,
    }
    st.session_state[ANALYSIS_SCOPE_KEY] = scope
    return scope


def get_analysis_scope_signature() -> str:
    scope = get_analysis_scope()
    return f"{scope.get('keyword', '')}|{scope.get('date_from')}|{scope.get('date_to')}"
