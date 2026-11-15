"""The executor adapter interface. An adapter runs one step of work and returns the final text."""
from __future__ import annotations


class ExecResult:
    def __init__(self, ok, text='', session_id=None, error=None, duration=0.0, timed_out=False, canceled=False):
        self.ok, self.text, self.session_id, self.error = ok, text, session_id, error
        self.duration, self.timed_out, self.canceled = duration, timed_out, canceled


class Executor:
    """One way of running an agent.

    caps says what the agent can do natively:
      resume          continue a session by id
      assign_session  accept a session id chosen by the caller
      schema          enforce a JSON Schema on the final answer
      images          take image files with the prompt
    The engine degrades for whatever is missing (design §6).
    """
    name = 'base'
    caps = {'resume': False, 'assign_session': False, 'schema': False, 'images': False}

    def run(self, prompt, *, work_dir, timeout, schema=None, session=None, mode='task',
            images=(), cancel=None):
        """Run one step. mode is 'task' (the configured permissions) or 'review' (read only, nothing
        kept). session, when given and resume is supported, continues that session."""
        raise NotImplementedError

    def selftest(self, work_dir):
        """A cheap check that the agent can be started and answers."""
        raise NotImplementedError

    def describe(self):
        return {'name': self.name, 'caps': dict(self.caps)}
