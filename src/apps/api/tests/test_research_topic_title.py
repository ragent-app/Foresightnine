"""The topic is named by a short title summarised from its first question."""
import json

from app.model_protocol import ModelResponse, ModelUsage
from app.research_topic_title import clean_title, parse_title
from tests.test_research_multiturn import Q1, Q2, Scripted, env, read_all, submit  # noqa: F401  (env is a fixture)


class Titled(Scripted):
    """Answers the title call with a fixed title and counts it."""

    def __init__(self, *steps, title='营收对比', **kwargs):
        super().__init__(*steps, **kwargs)
        self.title = title
        self.title_calls = []

    def complete(self, messages, tools, **kwargs):
        if not tools and 'question' in json.loads(messages[1]['content']):
            self.title_calls.append(json.loads(messages[1]['content'])['question'])
            content = self.title if self.title.startswith('{') else json.dumps({'title': self.title}, ensure_ascii=False)
            return ModelResponse({'role': 'assistant', 'content': content}, (), ModelUsage(5, 3, 8), 'stop')
        return super().complete(messages, tools, **kwargs)


def test_titles_are_cleaned_and_parsed():
    assert parse_title('{"title": "“NVIDIA 当日行情原因”。"}') == 'NVIDIA 当日行情原因'
    assert parse_title('```json\n{"title": "Revenue check"}\n```') == 'Revenue check'
    assert parse_title('标题：英伟达复职事件') == '英伟达复职事件'
    assert parse_title('{"other": 1}') is None
    assert parse_title('') is None
    assert clean_title('x' * 100) == 'x' * 40


def test_first_question_names_the_topic_once_and_the_call_is_accounted(env):
    sid = env.source_id
    provider = Titled(read_all(sid), submit(sid), read_all(sid), submit(sid, quotation='Revenue was 80 USD.'))
    env.install(provider)
    env.run()
    topic = env.client.get(env.path, headers=env.headers).json()
    assert topic['title'] == '营收对比'
    assert provider.title_calls == [Q1]
    first = topic['versions'][-1]['content']['turns'][0]
    assert first['question'] == Q1, 'the question itself is kept as asked'
    assert first['usage']['model_calls'] == 4, 'read, submit, the independent review, and the title call'
    # A follow-up does not rename the topic.
    env.run({'question_override': Q2})
    assert provider.title_calls == [Q1]
    assert env.client.get(env.path, headers=env.headers).json()['title'] == '营收对比'


def test_a_title_chosen_by_the_user_is_never_replaced(env):
    renamed = env.client.patch(env.path, headers=env.headers, json={'title': '我的研究'})
    assert renamed.status_code == 200, renamed.text
    sid = env.source_id
    provider = Titled(read_all(sid), submit(sid))
    env.install(provider)
    env.run()
    assert provider.title_calls == []
    assert env.client.get(env.path, headers=env.headers).json()['title'] == '我的研究'


def test_editing_the_first_question_renames_a_summarised_title(env):
    sid = env.source_id
    env.install(Titled(read_all(sid), submit(sid)))
    env.run()
    first = env.latest()['content']['turns'][0]
    provider = Titled(read_all(sid), submit(sid), title='成本对比')
    env.install(provider)
    env.run({'replace_turn_id': first['turn_id'], 'question_override': 'What were costs?'})
    assert provider.title_calls == ['What were costs?']
    assert env.client.get(env.path, headers=env.headers).json()['title'] == '成本对比'


def test_a_failed_or_empty_title_leaves_the_research_untouched(env):
    sid = env.source_id
    provider = Titled(read_all(sid), submit(sid), title='{"title": ""}')
    env.install(provider)
    env.run()
    topic = env.client.get(env.path, headers=env.headers).json()
    assert topic['title'] is None
    assert topic['versions'][-1]['content']['turns'][0]['status'] == 'completed'


def test_a_user_budget_is_not_spent_on_the_title(env):
    sid = env.source_id
    provider = Titled(read_all(sid), submit(sid))
    env.install(provider)
    env.run({'max_model_calls': 5})
    assert provider.title_calls == []
    assert env.client.get(env.path, headers=env.headers).json()['title'] is None


def test_progress_reports_the_topic_title(env):
    sid = env.source_id
    env.install(Titled(read_all(sid), submit(sid)))
    operation_id = env.run()
    progress = env.client.get(f"{env.path}/runs/{operation_id}/progress", headers=env.headers)
    assert progress.status_code == 200, progress.text
    assert progress.json()['topic_title'] == '营收对比'


def test_a_title_call_already_started_is_never_sent_again():
    from types import SimpleNamespace
    from app.research_topic_title import summarise_title

    class Never:
        def complete_turn(self, *args, **kwargs):
            raise AssertionError('must not call the model again')

    for status in ('started', 'failed', 'done', 'skipped_user_budget'):
        state = {'_topic_title': {'status': status}}
        summarise_title(state=state, model=Never(), context=SimpleNamespace(), save=lambda: None, question='Q', on_title=None)
        assert state['_topic_title'] == {'status': status}
