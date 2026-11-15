"""The sentences taskd itself writes to requesters, in the language of the task's goal."""
from __future__ import annotations

import re

CJK = re.compile(r'[㐀-鿿]')

TEXT = {
    'accepted': ('已接收任务 {task},正在规划。', 'Task {task} accepted; planning.'),
    'queued': ('已接收任务 {task},前面还有 {n} 个任务,排队中。', 'Task {task} accepted; {n} task(s) ahead, queued.'),
    'plan_ready': ('计划已拟定,共 {n} 个阶段,开始执行:\n{phases}', 'Plan ready, {n} phase(s); starting:\n{phases}'),
    'plan_approval': ('计划已拟定,共 {n} 个阶段,需要你确认后才执行:\n{phases}\n需要确认的原因:\n{reasons}\n同意请发送 {{"taskd":"approve_plan"}}。',
                      'Plan ready, {n} phase(s); it needs your approval first:\n{phases}\nWhy:\n{reasons}\nSend {{"taskd":"approve_plan"}} to go ahead.'),
    'phase_done': ('阶段 {phase} 已通过检查。', 'Phase {phase} passed its checks.'),
    'progress': ('进行中:阶段 {index}/{total}「{title}」,第 {steps} 步。{summary}', 'Working: phase {index}/{total} "{title}", step {steps}. {summary}'),
    'needs_user': ('需要你决定:{question}\n回答请发送 {{"taskd":"answer","text":"…"}}。', 'Your decision is needed: {question}\nAnswer with {{"taskd":"answer","text":"…"}}.'),
    'blocked': ('任务受阻:{reason}\n可以发送 {{"taskd":"hint","text":"…"}} 给出新的方向,或者处理外部条件后发送 {{"taskd":"answer","text":"已处理"}}。',
                'Blocked: {reason}\nSend {{"taskd":"hint","text":"…"}} with a new direction, or fix the external condition and send {{"taskd":"answer","text":"done"}}.'),
    'completed': ('任务已完成,全部检查通过。{summary}', 'Task completed; all checks passed. {summary}'),
    'completed_deferred': ('任务已完成,程序检查全部通过;有 {n} 项只能由你确认:{items}。{summary}',
                           'Task completed; all program checks passed; {n} item(s) need your confirmation: {items}. {summary}'),
    'stopped_budget': ('任务已停止:{reason}。已完成 {done}/{total} 个阶段。', 'Task stopped: {reason}. {done}/{total} phase(s) done.'),
    'canceled': ('任务已按你的要求取消。', 'Task canceled as you asked.'),
    'rejected': ('无法接收这个任务:{reason}', 'Cannot take this task: {reason}'),
    'status': ('当前状态:{state}。{detail}', 'State: {state}. {detail}'),
    'hint_taken': ('已收到提示,下一步会换一个会话按这个方向继续。', 'Hint received; the next step starts a fresh session with it.'),
    'answer_taken': ('已收到回答,继续执行。', 'Answer received; continuing.'),
    'nothing_to_answer': ('当前没有等待回答的问题,已作为提示记录。', 'No question is pending; recorded as a hint.'),
}

REASONS = {
    'no_progress': ('连续多步没有新的进展,重新规划后仍然如此', 'several steps made no progress, also after re-planning'),
    'executor_failed': ('执行器连续失败', 'the executor failed repeatedly'),
    'plan_failed': ('无法得到有效的计划', 'no usable plan could be made'),
    'verification_failed': ('多次验收未通过', 'verification failed repeatedly'),
    'environment': ('运行环境不可用:{detail}', 'the environment is unavailable: {detail}'),
    'budget_steps': ('步数预算已用完', 'the step budget is used up'),
    'budget_time': ('时间预算已用完', 'the time budget is used up'),
    'executor_blocked': ('执行器报告外部阻塞:{detail}', 'the executor reports an external blocker: {detail}'),
}


def lang(goal):
    return 0 if CJK.search(goal or '') else 1


def say(key, goal, **values):
    return TEXT[key][lang(goal)].format(**values)


def reason(key, goal, **values):
    text = REASONS.get(key)
    if not text:
        return key
    return text[lang(goal)].format(**values)
