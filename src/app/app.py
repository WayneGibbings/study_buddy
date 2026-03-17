"""Study Buddy — Databricks certification exam practice app.

User identity is read from the x-forwarded-access-token header (Databricks
App OAuth). All database writes use the app's service principal (app auth via
Config()), which the Databricks App runtime auto-injects.
"""

import streamlit as st

import database as db
import questions_loader as ql

st.set_page_config(page_title="Study Buddy", page_icon="📚", layout="wide")


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Source+Serif+4:ital,wght@0,300;0,400;0,600;1,400&family=JetBrains+Mono:wght@400;600&display=swap');

        html, body, [class*="css"] {
            font-family: 'Source Serif 4', Georgia, serif;
        }

        h1, h2, h3, h4, h5, h6,
        .stButton > button,
        .stSelectbox label,
        .stRadio label,
        [data-testid="stMetricLabel"],
        [data-testid="stMetricValue"],
        [data-testid="stCaptionContainer"],
        .stProgress,
        .stDataFrame {
            font-family: 'Syne', sans-serif !important;
        }

        #MainMenu, footer { visibility: hidden; }

        /* ── Question navigator grid ─────────────────────────── */
        .q-grid {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 3px;
            margin: 6px 0 12px 0;
        }
        .q-cell {
            border-radius: 5px;
            padding: 6px 2px;
            text-align: center;
            font-size: 10px;
            font-weight: 700;
            color: white;
            font-family: 'JetBrains Mono', monospace;
            cursor: default;
            line-height: 1.2;
        }
        .q-current {
            background: #2563eb;
            box-shadow: 0 0 0 2px #93c5fd;
        }
        .q-correct  { background: #15803d; }
        .q-wrong    { background: #b91c1c; }
        .q-pending  { background: #1e293b; color: #64748b; }

        /* ── Legend ──────────────────────────────────────────── */
        .q-legend {
            display: flex;
            flex-wrap: wrap;
            gap: 8px 16px;
            font-family: 'Syne', sans-serif;
            font-size: 11px;
            color: #94a3b8;
            margin-bottom: 4px;
        }
        .q-legend span {
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }
        .q-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 3px;
            flex-shrink: 0;
        }

        /* ── Exam header bar ─────────────────────────────────── */
        .exam-header {
            background: linear-gradient(135deg, #0f2044 0%, #1e293b 100%);
            border: 1px solid rgba(59, 130, 246, 0.25);
            border-radius: 10px;
            padding: 10px 18px;
            margin-bottom: 14px;
        }
        .exam-header-name {
            font-family: 'Syne', sans-serif;
            font-size: 15px;
            font-weight: 700;
            color: #e2e8f0;
            margin: 0;
            line-height: 1.3;
        }
        .exam-header-version {
            font-family: 'Syne', sans-serif;
            font-size: 11px;
            color: #64748b;
            margin: 1px 0 0 0;
        }

        /* ── Review mode banner ──────────────────────────────── */
        .review-banner {
            background: rgba(245, 158, 11, 0.08);
            border-left: 3px solid #f59e0b;
            border-radius: 0 8px 8px 0;
            padding: 8px 14px;
            margin-bottom: 16px;
            font-family: 'Syne', sans-serif;
            font-size: 12px;
            font-weight: 600;
            color: #fbbf24;
            letter-spacing: 0.03em;
        }

        /* ── Code blocks in question text ────────────────────── */
        code, pre, .stCode {
            font-family: 'JetBrains Mono', monospace !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Engine (cached for the lifetime of the app server)
# ---------------------------------------------------------------------------


@st.cache_resource
def _get_engine():
    engine = db.get_engine()
    db.init_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_current_user() -> dict:
    """Identify the logged-in user.

    Uses x-forwarded-user, which the Databricks App platform injects for every
    authenticated request without requiring On-Behalf-Of User Authorization.
    Falls back to 'dev_user' when running locally.
    """
    username = st.context.headers.get("x-forwarded-user")
    if not username:
        return {"username": "dev_user", "display_name": "Dev User"}
    display_name = username.split("@")[0].replace(".", " ").title()
    return {"username": username, "display_name": display_name}


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults = {
        "page": "home",
        "user": None,
        "current_session_id": None,
        "current_exam": None,
        "question_index": 0,
        "show_explanation": False,
        "last_answer_result": None,
        "selected_file": None,
        # {q_idx: {selected, correct, is_correct, explanation}} — in-memory
        # record of answers for the current session, used by the navigator.
        "answered_questions": {},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ---------------------------------------------------------------------------
# Sidebar — question navigator (shown only during an exam)
# ---------------------------------------------------------------------------


def _render_exam_sidebar() -> None:
    if st.session_state.get("page") != "exam":
        return
    exam = st.session_state.get("current_exam")
    if not exam:
        return

    questions = exam["questions"]
    total = len(questions)
    current_idx = st.session_state["question_index"]
    answered: dict = st.session_state["answered_questions"]
    session_id = st.session_state["current_session_id"]

    n_answered = len(answered)
    n_correct = sum(1 for v in answered.values() if v["is_correct"])

    # ── Exam identity ────────────────────────────────────────────────────
    exam_name = exam.get("exam", "Exam")
    version = exam.get("version", "")
    st.sidebar.markdown(f"**{exam_name}**")
    if version:
        st.sidebar.caption(version)
    st.sidebar.divider()

    # ── Progress stats ───────────────────────────────────────────────────
    col_a, col_b = st.sidebar.columns(2)
    col_a.metric("Answered", f"{n_answered}/{total}")
    col_b.metric("Correct", f"{n_correct}/{n_answered}" if n_answered else "—")
    st.sidebar.progress(n_answered / total if total else 0)
    st.sidebar.divider()

    # ── Visual question grid ─────────────────────────────────────────────
    st.sidebar.markdown("**Questions**")
    cells = ""
    for i in range(total):
        if i == current_idx:
            cls = "q-current"
            label = f"▶{i + 1}"
        elif i in answered:
            cls = "q-correct" if answered[i]["is_correct"] else "q-wrong"
            label = str(i + 1)
        else:
            cls = "q-pending"
            label = str(i + 1)
        cells += f'<div class="q-cell {cls}">{label}</div>'

    st.sidebar.markdown(
        f'<div class="q-grid">{cells}</div>',
        unsafe_allow_html=True,
    )

    st.sidebar.markdown(
        '<div class="q-legend">'
        '<span><span class="q-dot" style="background:#2563eb"></span>Current</span>'
        '<span><span class="q-dot" style="background:#15803d"></span>Correct</span>'
        '<span><span class="q-dot" style="background:#b91c1c"></span>Wrong</span>'
        '<span><span class="q-dot" style="background:#1e293b;border:1px solid #334155"></span>Pending</span>'
        "</div>",
        unsafe_allow_html=True,
    )
    st.sidebar.divider()

    # ── Jump-to navigation ───────────────────────────────────────────────
    st.sidebar.markdown("**Jump to Question**")

    options = []
    for i in range(total):
        if i == current_idx:
            prefix = "▶"
        elif i in answered:
            prefix = "✓" if answered[i]["is_correct"] else "✗"
        else:
            prefix = "○"
        options.append(f"{prefix}  Q{i + 1}")

    target_label = st.sidebar.selectbox(
        "Select question",
        options,
        index=current_idx,
        key=f"nav_select_{session_id}",
        label_visibility="collapsed",
    )
    target_idx = options.index(target_label)

    if st.sidebar.button("Go →", use_container_width=True):
        _navigate_to(target_idx, answered)


def _navigate_to(q_idx: int, answered: dict) -> None:
    """Navigate the exam to a specific question index."""
    st.session_state["question_index"] = q_idx
    if q_idx in answered:
        st.session_state["show_explanation"] = True
        st.session_state["last_answer_result"] = answered[q_idx]
    else:
        st.session_state["show_explanation"] = False
        st.session_state["last_answer_result"] = None
    st.rerun()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def render_home() -> None:
    user = st.session_state["user"]
    st.title("📚 Study Buddy")
    st.subheader(f"Welcome, {user['display_name']}!")
    st.markdown("Prepare for Databricks certification exams with practice questions.")
    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🎯 Start Exam", use_container_width=True, type="primary"):
            st.session_state["page"] = "exam_select"
            st.rerun()
    with col2:
        if st.button("📋 View History", use_container_width=True):
            st.session_state["page"] = "history"
            st.rerun()


def render_exam_select() -> None:
    st.title("📚 Select an Exam")
    if st.button("← Back"):
        st.session_state["page"] = "home"
        st.rerun()

    st.divider()
    exam_files = ql.list_exam_files()

    if not exam_files:
        st.warning("No exam files found in the questions directory.")
        return

    for ef in exam_files:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"**{ef['exam_name']}**")
            st.caption(f"{ef['total_questions']} questions · {ef['filename']}")
        with col2:
            if st.button("Start", key=f"start_{ef['filename']}", type="primary"):
                engine = _get_engine()
                session_id = db.create_session(
                    engine,
                    user_id=st.session_state["user"]["user_id"],
                    exam_name=ef["exam_name"],
                    exam_file=ef["filename"],
                    total_q=ef["total_questions"],
                )
                st.session_state["selected_file"] = ef
                st.session_state["current_session_id"] = session_id
                st.session_state["current_exam"] = ql.load_exam(ef["path"])
                st.session_state["question_index"] = 0
                st.session_state["show_explanation"] = False
                st.session_state["last_answer_result"] = None
                st.session_state["answered_questions"] = {}
                st.session_state["page"] = "exam"
                st.rerun()
        st.divider()


def render_exam() -> None:
    engine = _get_engine()
    exam = st.session_state["current_exam"]
    questions = exam["questions"]
    total = len(questions)
    idx = st.session_state["question_index"]
    session_id = st.session_state["current_session_id"]
    answered: dict = st.session_state["answered_questions"]

    # Completion guard (handles edge case where idx overshoots)
    if idx >= total:
        db.finalize_session(engine, session_id, "completed")
        st.session_state["page"] = "score"
        st.rerun()
        return

    # ── Header: exam name (left) + Quit button (right) ──────────────────
    exam_name = exam.get("exam", "Exam")
    version = exam.get("version", "")
    version_str = f'<p class="exam-header-version">{version}</p>' if version else ""

    hdr_col, quit_col = st.columns([8, 1])
    with hdr_col:
        st.markdown(
            f'<div class="exam-header">'
            f'<p class="exam-header-name">{exam_name}</p>'
            f"{version_str}"
            f"</div>",
            unsafe_allow_html=True,
        )
    with quit_col:
        # Align the quit button vertically with the header box
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        if st.button("🚪 Quit", key="quit_top", use_container_width=True):
            db.finalize_session(engine, session_id, "quit")
            st.session_state["page"] = "score"
            st.rerun()

    # ── Progress ─────────────────────────────────────────────────────────
    st.progress(idx / total, text=f"Question {idx + 1} of {total}")

    # ── Question content ─────────────────────────────────────────────────
    question = questions[idx]
    options = question["options"]
    option_keys = sorted(options.keys())

    section_title = question.get("section_title") or "General"
    st.caption(f"Section: {section_title}")
    st.markdown(f"**Question {idx + 1} of {total}**")
    st.markdown(question["question_text"])

    is_reviewing = idx in answered

    if is_reviewing:
        # ── Review mode: viewing a previously answered question ───────────
        st.markdown(
            '<div class="review-banner">👁  Reviewing your previous answer</div>',
            unsafe_allow_html=True,
        )
        result = answered[idx]
        _render_answer_reveal(options, option_keys, result)

        st.divider()
        nav_l, nav_m, nav_r = st.columns(3)
        with nav_l:
            if idx > 0 and st.button("← Previous", use_container_width=True):
                _navigate_to(idx - 1, answered)
        with nav_m:
            first_unanswered = next(
                (i for i in range(total) if i not in answered), None
            )
            if first_unanswered is not None:
                if st.button(
                    "▶ Resume Exam",
                    use_container_width=True,
                    type="primary",
                ):
                    _navigate_to(first_unanswered, answered)
        with nav_r:
            if idx + 1 < total and st.button("Next →", use_container_width=True):
                _navigate_to(idx + 1, answered)

    elif not st.session_state["show_explanation"]:
        # ── Answering phase ───────────────────────────────────────────────
        option_labels = [f"**{k}** — {options[k]}" for k in option_keys]
        selected_label = st.radio(
            "Choose your answer:",
            option_labels,
            index=None,
            key=f"q_{session_id}_{idx}",
        )

        st.divider()
        if st.button(
            "Submit Answer",
            disabled=selected_label is None,
            type="primary",
            use_container_width=True,
        ):
            selected_letter = selected_label.split("**")[1]
            correct_letter = question["correct_answer"]
            is_correct = selected_letter == correct_letter

            db.save_answer(engine, session_id, question, selected_letter, is_correct)

            result = {
                "selected": selected_letter,
                "correct": correct_letter,
                "is_correct": is_correct,
                "explanation": question.get("explanation", ""),
            }
            st.session_state["answered_questions"][idx] = result
            st.session_state["show_explanation"] = True
            st.session_state["last_answer_result"] = result
            st.rerun()

    else:
        # ── Explanation phase: just submitted ─────────────────────────────
        result = st.session_state["last_answer_result"]
        _render_answer_reveal(options, option_keys, result)

        st.divider()
        is_last = (idx + 1) >= total
        next_label = "✅ Finish Exam" if is_last else "Next Question →"
        if st.button(next_label, type="primary", use_container_width=True):
            new_idx = idx + 1
            st.session_state["question_index"] = new_idx
            st.session_state["show_explanation"] = False
            st.session_state["last_answer_result"] = None
            if new_idx >= total:
                db.finalize_session(engine, session_id, "completed")
                st.session_state["page"] = "score"
            st.rerun()


def _render_answer_reveal(options: dict, option_keys: list, result: dict) -> None:
    """Render the answer options with correct/incorrect highlighting."""
    for k in option_keys:
        if k == result["correct"] and k == result["selected"]:
            st.markdown(f"✅ **{k}:** {options[k]}")
        elif k == result["correct"]:
            st.markdown(f"✅ **{k}:** {options[k]}")
        elif k == result["selected"]:
            st.markdown(f"❌ **{k}:** {options[k]}")
        else:
            st.markdown(f"&ensp;**{k}:** {options[k]}")

    st.divider()
    if result["is_correct"]:
        st.success("✓ Correct!")
    else:
        st.error(
            f"✗ Incorrect — correct answer was **{result['correct']}**: "
            f"{options[result['correct']]}"
        )

    if result.get("explanation"):
        with st.container(border=True):
            st.markdown(f"**Explanation:** {result['explanation']}")


def render_score() -> None:
    engine = _get_engine()
    session_id = st.session_state["current_session_id"]

    session_info = db.get_session_info(engine, session_id)
    section_scores = db.get_section_scores(engine, session_id)

    total_q = sum(s["total_questions"] for s in section_scores)
    total_correct = sum(s["correct_answers"] for s in section_scores)
    overall_pct = (100.0 * total_correct / total_q) if total_q > 0 else 0.0

    st.title("📊 Exam Results")

    if session_info:
        icon = "✅" if session_info["status"] == "completed" else "⚠️"
        st.caption(
            f"{icon} {session_info['status'].title()} · {session_info['exam_name']}"
        )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Overall Score", f"{overall_pct:.1f}%")
    with col2:
        st.metric("Correct Answers", f"{total_correct}/{total_q}")
    with col3:
        pass_threshold = "Pass ≥ 70%" if overall_pct >= 70 else "Below 70%"
        st.metric("Threshold", pass_threshold)

    st.divider()
    st.subheader("Score by Section")

    if section_scores:
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "Section": s["section_title"],
                    "Questions": int(s["total_questions"]),
                    "Correct": int(s["correct_answers"]),
                    "Score": f"{s['score_pct']}%",
                }
                for s in section_scores
            ]
        )
        st.dataframe(df, hide_index=True, use_container_width=True)
    else:
        st.info("No answers were recorded for this session.")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🏠 Home", use_container_width=True):
            st.session_state["page"] = "home"
            st.rerun()
    with col2:
        if st.button("🔄 Retake Exam", use_container_width=True, type="primary"):
            selected_file = st.session_state.get("selected_file")
            if selected_file:
                new_session_id = db.create_session(
                    engine,
                    user_id=st.session_state["user"]["user_id"],
                    exam_name=selected_file["exam_name"],
                    exam_file=selected_file["filename"],
                    total_q=selected_file["total_questions"],
                )
                st.session_state["current_session_id"] = new_session_id
                st.session_state["question_index"] = 0
                st.session_state["show_explanation"] = False
                st.session_state["last_answer_result"] = None
                st.session_state["answered_questions"] = {}
                st.session_state["page"] = "exam"
            else:
                st.session_state["page"] = "exam_select"
            st.rerun()


def render_history() -> None:
    engine = _get_engine()
    user_id = st.session_state["user"]["user_id"]

    st.title("📋 Exam History")
    if st.button("← Back"):
        st.session_state["page"] = "home"
        st.rerun()

    st.divider()
    history = db.get_user_history(engine, user_id)

    if not history:
        st.info("No exam history yet. Start your first exam!")
        return

    for session in history:
        status = session["status"]
        icon = "✅" if status == "completed" else ("⚠️" if status == "quit" else "🔄")
        score_str = f"{session['score_pct']}%" if session["score_pct"] is not None else "—"
        answered = int(session["answered_count"] or 0)
        total = int(session["total_questions"])
        date_str = (
            session["started_at"].strftime("%Y-%m-%d %H:%M")
            if session["started_at"]
            else "Unknown date"
        )

        with st.expander(
            f"{icon} {session['exam_name']} — {score_str} "
            f"({answered}/{total}) — {date_str}"
        ):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Score", score_str)
            with col2:
                st.metric("Answered", f"{answered}/{total}")
            with col3:
                st.metric("Status", status.title())

            section_scores = db.get_section_scores(engine, session["session_id"])
            if section_scores:
                import pandas as pd

                df = pd.DataFrame(
                    [
                        {
                            "Section": s["section_title"],
                            "Correct": int(s["correct_answers"]),
                            "Total": int(s["total_questions"]),
                            "Score": f"{s['score_pct']}%",
                        }
                        for s in section_scores
                    ]
                )
                st.dataframe(df, hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _inject_css()
    _init_state()
    _render_exam_sidebar()

    # Identify and register user on first load
    if st.session_state["user"] is None:
        with st.spinner("Connecting…"):
            user_info = _get_current_user()
            engine = _get_engine()
            user_id = db.upsert_user(
                engine, user_info["username"], user_info["display_name"]
            )
            st.session_state["user"] = {**user_info, "user_id": user_id}

    page = st.session_state["page"]
    if page == "home":
        render_home()
    elif page == "exam_select":
        render_exam_select()
    elif page == "exam":
        render_exam()
    elif page == "score":
        render_score()
    elif page == "history":
        render_history()
    else:
        st.session_state["page"] = "home"
        st.rerun()


if __name__ == "__main__":
    main()
