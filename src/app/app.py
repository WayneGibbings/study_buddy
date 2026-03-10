"""Study Buddy — Databricks certification exam practice app.

User identity is read from the x-forwarded-access-token header (Databricks
App OAuth). All database writes use the app's service principal (app auth via
Config()), which the Databricks App runtime auto-injects.
"""

import streamlit as st

import database as db
import questions_loader as ql

st.set_page_config(page_title="Study Buddy", page_icon="📚", layout="centered")


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
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


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

    # Completion guard (handles edge case where idx overshoots)
    if idx >= total:
        db.finalize_session(engine, session_id, "completed")
        st.session_state["page"] = "score"
        st.rerun()
        return

    st.progress(idx / total, text=f"Question {idx + 1} of {total}")

    question = questions[idx]
    options = question["options"]
    option_keys = sorted(options.keys())
    option_labels = [f"**{k}** — {options[k]}" for k in option_keys]

    st.caption(f"Section: {question['section_title']}")
    st.markdown(f"**Question {idx + 1} of {total}**")
    st.markdown(question["question_text"])

    if not st.session_state["show_explanation"]:
        # ── Answering phase ──────────────────────────────────────────────
        selected_label = st.radio(
            "Choose your answer:",
            option_labels,
            index=None,
            key=f"q_{session_id}_{idx}",
        )

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                "Submit Answer",
                disabled=selected_label is None,
                type="primary",
                use_container_width=True,
            ):
                # Extract letter from "**A** — ..." → split on ** gives ['', 'A', ' — ...']
                selected_letter = selected_label.split("**")[1]
                correct_letter = question["correct_answer"]
                is_correct = selected_letter == correct_letter

                db.save_answer(engine, session_id, question, selected_letter, is_correct)
                st.session_state["show_explanation"] = True
                st.session_state["last_answer_result"] = {
                    "selected": selected_letter,
                    "correct": correct_letter,
                    "is_correct": is_correct,
                    "explanation": question.get("explanation", ""),
                }
                st.rerun()
        with col2:
            if st.button("🚪 Quit Exam", use_container_width=True):
                db.finalize_session(engine, session_id, "quit")
                st.session_state["page"] = "score"
                st.rerun()

    else:
        # ── Explanation phase ────────────────────────────────────────────
        result = st.session_state["last_answer_result"]

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

        if result["explanation"]:
            with st.container(border=True):
                st.markdown(f"**Explanation:** {result['explanation']}")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
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
        with col2:
            if st.button("🚪 Quit Exam", use_container_width=True):
                db.finalize_session(engine, session_id, "quit")
                st.session_state["page"] = "score"
                st.rerun()


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
    _init_state()

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
