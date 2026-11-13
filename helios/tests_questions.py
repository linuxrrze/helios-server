"""
Server-side checks of the questions sent by the question editor (save_questions).
"""

import copy
import json
import uuid

from django.test import TestCase

from helios import models
from helios_auth import models as auth_models

VALID = {"answer_urls": [None, None], "answers": ["Yes", "No"], "choice_type": "approval", "max": 1, "min": 0,
         "question": "Test?", "result_type": "absolute", "short_name": "Test?", "tally_type": "homomorphic"}


def question(**changes):
  q = copy.deepcopy(VALID)
  for key, value in changes.items():
    if value is KeyError:
      del q[key]
    else:
      q[key] = value
  return q


class QuestionsProblemTests(TestCase):
  """Election.questions_problem: what may be saved"""

  def assertValid(self, questions):
    self.assertIsNone(models.Election.questions_problem(questions), questions)

  def assertProblem(self, questions, text=None):
    problem = models.Election.questions_problem(questions)
    self.assertIsNotNone(problem, questions)
    if text:
      self.assertIn(text, problem)

  def test_existing_formats_still_valid(self):
    # as the editor saves them, as in the fixtures and the legacy data
    for name in ['helios/fixtures/election.json', 'helios/fixtures/legacy-election-expected.json']:
      with open(name) as f:
        data = json.load(f)
      for obj in data if isinstance(data, list) else [data]:
        fields = obj.get('fields', obj)
        questions = fields.get('questions')
        if isinstance(questions, str):
          questions = json.loads(questions)
        if questions:
          self.assertValid(questions)
    self.assertValid([])
    self.assertValid([VALID])
    self.assertValid([question(max=None), question(max=None, min=2), question(min=KeyError)])
    self.assertValid([question(result_type="relative"), question(result_type=KeyError)])
    self.assertValid([question(answer_urls=["https://example.org/a", ""]), question(answer_urls=["http://example.org", None])])
    self.assertValid([question(randomize_answer_order=True, answers=["A", "B", "C"], answer_urls=[None, None, None], min=3, max=3)])
    # unknown keys and other choice/tally types are kept as they are
    self.assertValid([question(choice_type="other", tally_type="other", extra={"x": 1})])
    # empty fields next to real answers were possible with the old editor
    self.assertValid([question(answers=["Yes", ""])])

  def test_min_max_against_answers(self):
    self.assertProblem([question(min=2, max=1)], "cannot be more than the maximum")
    self.assertProblem([question(min=0, max=3)], "The maximum of 3 answers is more than the 2 answers")
    self.assertProblem([question(min=3, max=None)], "Voters cannot choose at least 3 answers: the question has only 2.")
    self.assertProblem([question(max=0)], "must be at least 1")
    self.assertProblem([question(min=-1)], "cannot be less than 0")

  def test_answers_required(self):
    self.assertProblem([question(answers=[], answer_urls=[])], "at least one answer option")
    self.assertProblem([question(answers=[" ", ""])], "at least one answer option")

  def test_names_the_question(self):
    self.assertProblem([VALID, question(max=5)], "Question 2:")

  def test_whole_numbers_only(self):
    for bad in ["1", 1.0, 1.5, True, None, [1]]:
      self.assertProblem([question(min=bad)], "not in the expected format")
    for bad in ["1", 1.5, False, {}]:
      self.assertProblem([question(max=bad)], "not in the expected format")

  def test_bad_format(self):
    for bad in [None, {}, "x", [None], ["x"], [question(max=KeyError)], [question(answers=KeyError)],
                [question(answer_urls=KeyError)], [question(answers=[1, 2])], [question(answer_urls=[1, None])],
                [question(result_type="majority")]]:
      self.assertProblem(bad, "not in the expected format")

  def test_answer_links(self):
    self.assertProblem([question(answer_urls=["javascript:alert(1)", None])], "must start with http:// or https://")
    self.assertProblem([question(answer_urls=["ftp://example.org", None])], "must start with http:// or https://")


class SaveQuestionsViewTests(TestCase):
  fixtures = ['users.json']

  def setUp(self):
    self.user = auth_models.User.objects.get(user_id='ben@adida.net', user_type='google')
    self.election, _ = models.Election.get_or_create(short_name='test-save-questions', name='Save questions',
                                                     description='', admin=self.user)
    self.election.uuid = str(uuid.uuid4())
    self.election.questions = [VALID]
    self.election.save()
    self.client.get('/')
    session = self.client.session
    session['user'] = {'type': self.user.user_type, 'user_id': self.user.user_id}
    session.save()
    self.url = '/helios/elections/%s/save_questions' % self.election.uuid

  def post(self, questions_json, **extra):
    return self.client.post(self.url, {'questions_json': questions_json, 'csrf_token': self.client.session['csrf_token']}, **extra)

  def stored(self):
    return models.Election.objects.get(id=self.election.id).questions

  def test_valid_questions_saved(self):
    new = [question(answers=["A", "B", "C"], answer_urls=[None, "https://example.org", ""], min=1, max=None, result_type="relative")]
    response = self.post(json.dumps(new))
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.content, b"SUCCESS")
    self.assertEqual(self.stored(), new)

  def test_invalid_questions_rejected_with_message(self):
    response = self.post(json.dumps([VALID, question(min=2, max=1)]))
    self.assertEqual(response.status_code, 400)
    self.assertEqual(response.json(), {'result': 'FAILURE', 'error': 'Question 2: The minimum number of answers cannot be more than the maximum.'})
    self.assertEqual(self.stored(), [VALID])

  def test_message_translated(self):
    response = self.post(json.dumps([question(max=3)]), HTTP_ACCEPT_LANGUAGE='de')
    self.assertEqual(response.status_code, 400)
    self.assertTrue(response.json()['error'].startswith('Frage 1: '), response.json()['error'])
    self.assertNotIn('maximum', response.json()['error'])

  def test_not_json(self):
    for payload in ['not json', '', '{"a": 1}']:
      response = self.post(payload)
      self.assertEqual(response.status_code, 400, payload)
      self.assertIn('not in the expected format', response.json()['error'])
    self.assertEqual(self.stored(), [VALID])

  def test_bad_link_rejected(self):
    response = self.post(json.dumps([question(answer_urls=["javascript:alert(1)", None])]))
    self.assertEqual(response.status_code, 400)
    self.assertIn('http:// or https://', response.json()['error'])
    self.assertEqual(self.stored(), [VALID])

  def test_editor_shows_server_message(self):
    content = self.client.get('/helios/elections/%s/questions' % self.election.uuid).content.decode()
    self.assertIn('id="question_editor_error"', content)
    self.assertIn('JSON.parse(xhr.responseText).error', content)
    self.assertNotIn('alert(TEXTS', content)
