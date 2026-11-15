"""Claude SDK permission callback backed by explicit per-request owner input."""
import copy

from .native_input import NativeInputClosed, NativeInputTimeout, _bounded


class ClaudeNativeInput:
    def __init__(self, broker, source_id, operation_id, native_id):
        self.broker = broker
        self.source_id = source_id
        self.operation_id = operation_id
        self.native_id = native_id

    @staticmethod
    def _decision(result):
        if not isinstance(result, dict) or set(result) != {'decision'} or result['decision'] not in ('accept', 'decline', 'cancel'):
            raise ValueError('choose one explicit native approval decision')
        return result

    @staticmethod
    def _questions(original):
        questions = original.get('questions')
        if not isinstance(questions, list) or not 1 <= len(questions) <= 16:
            raise ValueError('native questions are missing or exceed the supported limit')
        names = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError('native question must be an object')
            name = question.get('question')
            if not isinstance(name, str) or not name.strip() or len(name) > 4096 or name in names:
                raise ValueError('native question text must be unique and nonempty')
            names.add(name)
            if 'multiSelect' in question and type(question['multiSelect']) is not bool:
                raise ValueError('native question selection mode is invalid')
            options = question.get('options', [])
            if not isinstance(options, list) or len(options) > 32:
                raise ValueError('native question options are invalid')
            labels = set()
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get('label'), str) or not option['label'].strip() or len(option['label']) > 1024 or option['label'] in labels:
                    raise ValueError('native question options require unique labels')
                labels.add(option['label'])
        return questions

    @classmethod
    def _answers_validator(cls, original):
        questions = cls._questions(original)
        expected = {question['question']: question for question in questions}

        def validate(result):
            if isinstance(result, dict) and result == {'decision': 'cancel'}:
                return result
            if not isinstance(result, dict) or set(result) != {'answers'} or not isinstance(result['answers'], dict) or set(result['answers']) != set(expected):
                raise ValueError('answer every exact native question once')
            for name, answer in result['answers'].items():
                # The SDK accepts free text, or a list for a multi-select question.
                if isinstance(answer, str):
                    if not answer.strip() or len(answer) > 4096:
                        raise ValueError('native question answer must be nonempty')
                elif isinstance(answer, list) and expected[name].get('multiSelect') is True:
                    labels = {option['label'] for option in expected[name].get('options', [])}
                    if not answer or len(answer) > 32 or not all(isinstance(value, str) and value in labels for value in answer) or len(set(answer)) != len(answer):
                        raise ValueError('choose distinct original native options')
                else:
                    raise ValueError('native question answer has the wrong type')
            return result
        return validate

    async def __call__(self, tool_name, input_data, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        try:
            if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 256:
                raise ValueError('native tool name is invalid')
            tool_id = getattr(context, 'tool_use_id', None)
            if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 256:
                raise ValueError('native tool call identity is missing')
            if not isinstance(input_data, dict):
                raise ValueError('native tool input must be an object')
            original = _bounded(input_data)
            question = tool_name == 'AskUserQuestion'
            detail = {'tool_name': tool_name, 'tool_use_id': tool_id,
                      'input': copy.deepcopy(original),
                      'operation_correlation_verified': False,
                      'binding_scope': 'owned_native_session_local_activity_binding'}
            for field in ('agent_id', 'blocked_path', 'decision_reason', 'title', 'display_name', 'description'):
                value = getattr(context, field, None)
                if isinstance(value, str) and value:
                    detail[field] = value
            if question:
                detail['questions'] = copy.deepcopy(self._questions(original))
                validator = self._answers_validator(original)
            else:
                validator = self._decision
            result = await self.broker.ask_async(
                self.source_id, self.operation_id, self.native_id,
                'claude/tool/requestUserInput' if question else 'claude/tool/requestApproval',
                detail, validator)
            if not self.broker.is_operation_open(self.source_id,self.operation_id):
                return PermissionResultDeny(message='The original native activity has ended.',interrupt=True)
            if question:
                if result.get('decision') == 'cancel':
                    return PermissionResultDeny(message='The owner cancelled these questions.', interrupt=True)
                # Keep every native input field and its original question text.
                original['answers'] = result['answers']
                return PermissionResultAllow(updated_input=original)
            if result['decision'] == 'accept':
                return PermissionResultAllow(updated_input=original)
            return PermissionResultDeny(message='The owner declined this tool call.',
                                        interrupt=result['decision'] == 'cancel')
        except (NativeInputClosed, NativeInputTimeout, ValueError, TypeError):
            # Missing owner input never becomes an allow response or new rules.
            return PermissionResultDeny(message='An explicit owner decision was unavailable.', interrupt=True)
