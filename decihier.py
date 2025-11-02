import json
import uuid
import os
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from datetime import datetime

# --- 1. CONFIGURATION AND DATA LOADING (UPDATED) ---

TRAIT_MAP = {
    "TA": "Technical Acumen", "SL": "Strategic Leadership", "ER": "Ethical Responsibility",
    "BP": "Business Profitability", "BS": "Behavioral Speed",
}
# UPDATED TIME THRESHOLDS: Max 1 minute per question. Increased penalties for deviation.
TIME_THRESHOLDS = {
    "TOO_FAST": 15000,
    "OPTIMAL_MIN": 30000,
    "OPTIMAL_MAX": 60000,  # Max optimal time is 1 minute (60,000 ms)
    "TOO_SLOW": 60001,    # Anything over 1 minute is too slow
}
MAX_THEORETICAL_SCORE = 100

# In-memory storage for test results and generated tests (for hackathon persistence)
TEST_RESULTS = []
GENERATED_TESTS = {}


def load_assessment_data():
    """Loads all assessment data from the external questions.json file."""
    if not os.path.exists('questions.json'):
        print("\n[ERROR] 'questions.json' not found. Returning empty structure.")
        return {"PASS_THRESHOLD": 75, "JOB_PROFILES": {}}

    try:
        # CRITICAL FIX: Add encoding='utf-8' to handle special characters
        with open('questions.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(
            f"\n[INFO] Successfully loaded {len(data.get('JOB_PROFILES', {}))} job profiles.")
        return data
    except json.JSONDecodeError as e:
        print(
            f"\n[CRITICAL ERROR] 'questions.json' is invalid JSON. Error: {e}")
        return {"PASS_THRESHOLD": 75, "JOB_PROFILES": {}}
    except Exception as e:
        print(
            f"\n[UNEXPECTED ERROR] An error occurred loading questions.json: {e}")
        return {"PASS_THRESHOLD": 75, "JOB_PROFILES": {}}


ASSESSMENT_DATA = load_assessment_data()
app = Flask(__name__)
app.secret_key = str(uuid.uuid4())

# --- 2. CORE SCORING LOGIC (The psychology_predictor) (STRICTER LOGIC APPLIED) ---


def psychology_predictor(job_key, answers):
    """Calculates the WorkDNA score based on selected options and time taken, with STRICTER PENALTIES."""
    job_questions = None
    if session.get('test_id') in GENERATED_TESTS:
        job_questions = GENERATED_TESTS[session['test_id']]['questions']
    elif ASSESSMENT_DATA['JOB_PROFILES'].get(job_key):
        job_questions = ASSESSMENT_DATA['JOB_PROFILES'][job_key]['questions']

    if not job_questions:
        return None

    total_raw_score = 0
    skill_scores = {"TA": 0, "SL": 0, "ER": 0, "BP": 0, "BS": 0}

    all_possible_skills = set(TRAIT_MAP.keys())
    for skill in all_possible_skills:
        skill_scores[skill] = 0

    behavioral_summary = {
        "fastResponses": 0, "slowResponses": 0, "optimalResponses": 0, "totalTimeMs": 0,
    }
    detailed_results = []

    # Base for trait score normalization (Max 10 points per question for non-BS traits)
    max_possible_trait_score_base = 0

    for answer in answers:
        question = next(
            (q for q in job_questions if q['id'] == answer['questionId']), None)
        if not question:
            continue

        max_possible_trait_score_base += 10

        try:
            option_index = int(answer['selectedOptionIndex'])
            time_taken = int(answer['timeTakenMs'])
        except (ValueError, TypeError):
            continue

        if 0 <= option_index < len(question['options']):
            option = question['options'][option_index]
        else:
            continue

        behavioral_summary['totalTimeMs'] += time_taken
        question_points = 0
        time_behavior = 'Optimal'

        option_score = option['score']
        for trait_key, score in option_score.items():
            skill_scores[trait_key] += score
            question_points += score

        time_score = 0

        # --- STRICTER TIME PENALTY LOGIC ---
        if time_taken < TIME_THRESHOLDS['TOO_FAST']:
            # Too fast: increased penalty (-20)
            time_score = -20
            skill_scores['BS'] += 5
            skill_scores['ER'] -= 5
            skill_scores['SL'] -= 5
            behavioral_summary['fastResponses'] += 1
            time_behavior = 'Too Fast (Lack of Contemplation)'
        elif time_taken > TIME_THRESHOLDS['OPTIMAL_MAX']:
            # Too slow: increased penalty (-20)
            time_score = -20
            skill_scores['TA'] -= 5
            skill_scores['BS'] -= 10
            behavioral_summary['slowResponses'] += 1
            time_behavior = 'Too Slow (Inefficiency)'
        elif TIME_THRESHOLDS['OPTIMAL_MIN'] <= time_taken <= TIME_THRESHOLDS['OPTIMAL_MAX']:
            # Optimal: +5 bonus
            time_score = 5
            skill_scores['BS'] += 5
            behavioral_summary['optimalResponses'] += 1

        total_raw_score += (question_points + time_score)

        detailed_results.append({
            'questionId': answer['questionId'],
            'prompt': question['prompt'],
            'chosenOptionText': option['text'],
            'rawScoreImpact': option_score,
            'timeTakenMs': time_taken,
            'timeBehavior': time_behavior,
            'traitMap': TRAIT_MAP
        })

    # MAX_POSSIBLE_SCORE = Max points per question (10 for option + 5 for time) * number of questions
    MAX_POSSIBLE_SCORE = len(job_questions) * 15

    # Normalizes total raw score against the absolute best case scenario (15 points per question).
    # Clamps the result between 0 and 100.
    normalized_score = max(
        0, min(100, round((total_raw_score / MAX_POSSIBLE_SCORE) * 100)))

    # TRAIT SCORE NORMALIZATION
    for key in skill_scores:
        if key != 'BS':
            if max_possible_trait_score_base > 0:
                score_val = round(
                    (skill_scores[key] / max_possible_trait_score_base) * 100)
            else:
                score_val = 0
            skill_scores[key] = max(0, min(100, score_val))
        else:
            # BS score is clamped between 0 and 100 as it's a cumulative behavioral metric.
            skill_scores[key] = max(0, min(100, skill_scores[key]))

    return {
        "totalScore": normalized_score,
        "skillScores": skill_scores,
        "behavioralSummary": behavioral_summary,
        "detailedResults": detailed_results,
        "jobTitle": job_key,
        "candidateName": session.get('candidate_name', 'N/A'),
        "testId": session.get('test_id', 'N/A'),
        "passThreshold": ASSESSMENT_DATA['PASS_THRESHOLD'],
        "traitMap": TRAIT_MAP
    }

# --- 3. FLASK ROUTES: CANDIDATE FLOW (MODIFIED REDIRECT) ---


@app.route('/', methods=['GET'])
def index():
    """Renders the login/selection page."""
    job_profiles = ASSESSMENT_DATA['JOB_PROFILES']
    is_logged_in = 'candidate_name' in session and 'test_id' in session

    # Pass ASSESSMENT_DATA to prevent UndefinedError in index.html JS
    return render_template('index.html',
                           job_profiles=job_profiles,
                           is_logged_in=is_logged_in,
                           session=session,
                           ASSESSMENT_DATA=ASSESSMENT_DATA)


@app.route('/login', methods=['POST'])
def login():
    """Handles the candidate login with Name and Test ID."""
    name = request.form.get('name')
    test_id = request.form.get('test_id')

    if not name or not test_id:
        return render_template('index.html', error="Please provide a Name and Test ID.", is_logged_in=False, ASSESSMENT_DATA=ASSESSMENT_DATA)

    assigned_role = ""

    if test_id in GENERATED_TESTS:
        assigned_role = GENERATED_TESTS[test_id]['role']
    elif test_id != 'test123':
        return render_template('index.html', error="Invalid Test ID.", is_logged_in=False, ASSESSMENT_DATA=ASSESSMENT_DATA)

    session['candidate_name'] = name
    session['test_id'] = test_id
    session['assigned_role'] = assigned_role

    return redirect(url_for('index'))


@app.route('/logout', methods=['GET'])
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/get_assessment_details', methods=['POST'])
def get_assessment_details():
    """API endpoint to get skills and questions for the selected job."""
    job_key = request.json.get('jobKey')
    job_data = ASSESSMENT_DATA['JOB_PROFILES'].get(job_key)

    if job_data:
        return jsonify({
            'questions_count': len(job_data['questions']),
            'skills_required': job_data['skills_required'],
            'pass_threshold': ASSESSMENT_DATA['PASS_THRESHOLD'],
            'total_time_minutes': 10
        })
    return jsonify({"error": "Job profile not found"}), 404


# CRITICAL FIX: Use 'path' converter to allow slashes (/) in job_key
@app.route('/candidate/test/<path:job_key>', methods=['GET'])
def start_assessment_page(job_key):
    """Renders the actual timed assessment page."""
    if 'candidate_name' not in session:
        return redirect(url_for('index'))

    test_id = session.get('test_id')

    if test_id in GENERATED_TESTS:
        questions = GENERATED_TESTS[test_id]['questions']
    else:
        # job_key is unquoted here, matching the dict key structure
        job_data = ASSESSMENT_DATA['JOB_PROFILES'].get(job_key)
        if not job_data:
            return f"Job profile '{job_key}' not found in data. Check your JSON keys!", 404
        questions = job_data['questions']

    return render_template('candidate_test.html',
                           job_key=job_key,
                           candidate_name=session['candidate_name'],
                           questions=questions,
                           time_limit_minutes=10)


@app.route('/assess', methods=['POST'])
def assess():
    """Receives candidate answers, runs the predictor, stores results, and redirects."""
    if 'candidate_name' not in session:
        return jsonify({"error": "Session expired, please log in again."}), 401

    try:
        data = request.json
        job_key = data.get('jobKey')
        answers = data.get('answers')

        if not job_key or not answers:
            return jsonify({"error": "Missing job key or answers"}), 400

        results = psychology_predictor(job_key, answers)

        if not results:
            return jsonify({"error": "Job profile not found in data structure"}), 404

        result_id = str(uuid.uuid4())
        TEST_RESULTS.append({
            'id': result_id[:8],
            'username': results['candidateName'],
            'score': results['totalScore'],
            'role': results['jobTitle'],
            'submitted_at': datetime.utcnow().isoformat(),
            'full_results': results
        })

        session['last_result_id'] = result_id

        # IMPORTANT: Candidate session variables are cleared after submission
        session.pop('candidate_name', None)
        session.pop('test_id', None)
        session.pop('assigned_role', None)

        # UPDATED REDIRECT: Send candidate to the beautiful thank you page
        return jsonify({'redirect_url': url_for('thank_you_page')})

    except Exception as e:
        app.logger.error(f"Assessment failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/thankyou', methods=['GET'])
def thank_you_page():
    """Renders the new confirmation page for the candidate."""
    return render_template('thankyou.html')


# --- 4. FLASK ROUTES: HR FLOW (MODIFIED RESULTS ROUTE) ---


@app.route('/hrresults/<result_id>', methods=['GET'])
def show_assessment_result(result_id):
    """Renders the HR-only report by fetching the result from memory."""
    # Note: The function name remains the same for simplicity, but the route is changed
    result_entry = next(
        (r for r in TEST_RESULTS if r['id'] == result_id[:8] or r['full_results']['testId'] == result_id), None)

    if not result_entry:
        return "Result not found or session expired.", 404

    # Renders the HR-only report (renamed from output.html)
    return render_template('hr_report.html', result=result_entry['full_results'])


@app.route('/hr', methods=['GET', 'POST'])
def hr_create_test():
    """HR portal to generate a new test key for a role."""
    roles = list(ASSESSMENT_DATA['JOB_PROFILES'].keys())

    if request.method == 'POST':
        role = request.form.get('role')
        if role and role in ASSESSMENT_DATA['JOB_PROFILES']:
            test_key = str(uuid.uuid4())[:6]
            GENERATED_TESTS[test_key] = {
                'role': role,
                'questions': ASSESSMENT_DATA['JOB_PROFILES'][role]['questions']
            }
            return render_template('hr_success.html',
                                   role=role,
                                   test_key=test_key,
                                   questions=GENERATED_TESTS[test_key]['questions'])

    return render_template('hr.html', roles=roles)


@app.route('/hr/dashboard', methods=['GET'])
def hr_dashboard():
    """HR dashboard to view all submitted results."""
    sorted_results = sorted(
        TEST_RESULTS, key=lambda x: x['submitted_at'], reverse=True)
    return render_template('hr_dashboard.html', results=sorted_results)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
