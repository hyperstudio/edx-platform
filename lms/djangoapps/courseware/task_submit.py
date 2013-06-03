import json
import logging
from django.http import HttpResponse
from django.db import transaction

from celery.result import AsyncResult
from celery.states import READY_STATES

from courseware.models import CourseTaskLog
from courseware.module_render import get_xqueue_callback_url_prefix
from courseware.tasks import (rescore_problem,
                              reset_problem_attempts, delete_problem_state)
from xmodule.modulestore.django import modulestore


log = logging.getLogger(__name__)


class AlreadyRunningError(Exception):
    pass


def get_running_course_tasks(course_id):
    """
    Returns a query of CourseTaskLog objects of running tasks for a given course.

    Used to generate a list of tasks to display on the instructor dashboard.
    """
    course_tasks = CourseTaskLog.objects.filter(course_id=course_id)
    for state in READY_STATES:
        course_tasks = course_tasks.exclude(task_state=state)
    return course_tasks


def get_course_task_history(course_id, problem_url, student=None):
    """
    Returns a query of CourseTaskLog objects of historical tasks for a given course,
    that match a particular problem and optionally a student.
    """
    _, task_key = _encode_problem_and_student_input(problem_url, student)

    course_tasks = CourseTaskLog.objects.filter(course_id=course_id, task_key=task_key)
    return course_tasks.order_by('-id')


def course_task_log_status(request, task_id=None):
    """
    This returns the status of a course-related task as a JSON-serialized dict.

    The task_id can be specified in one of three ways:

    * explicitly as an argument to the method (by specifying in the url)
      Returns a dict containing status information for the specified task_id

    * by making a post request containing 'task_id' as a parameter with a single value
      Returns a dict containing status information for the specified task_id

    * by making a post request containing 'task_ids' as a parameter,
      with a list of task_id values.
      Returns a dict of dicts, with the task_id as key, and the corresponding
      dict containing status information for the specified task_id

      Task_id values that are unrecognized are skipped.

    """
    output = {}
    if task_id is not None:
        output = _get_course_task_log_status(task_id)
    elif 'task_id' in request.POST:
        task_id = request.POST['task_id']
        output = _get_course_task_log_status(task_id)
    elif 'task_ids[]' in request.POST:
        tasks = request.POST.getlist('task_ids[]')
        for task_id in tasks:
            task_output = _get_course_task_log_status(task_id)
            if task_output is not None:
                output[task_id] = task_output

    return HttpResponse(json.dumps(output, indent=4))


def _task_is_running(course_id, task_type, task_key):
    """Checks if a particular task is already running"""
    runningTasks = CourseTaskLog.objects.filter(course_id=course_id, task_type=task_type, task_key=task_key)
    for state in READY_STATES:
        runningTasks = runningTasks.exclude(task_state=state)
    return len(runningTasks) > 0


@transaction.autocommit
def _reserve_task(course_id, task_type, task_key, task_input, requester):
    """
    Creates a database entry to indicate that a task is in progress.

    An exception is thrown if the task is already in progress.

    Autocommit annotation makes sure the database entry is committed.
    """

    if _task_is_running(course_id, task_type, task_key):
        raise AlreadyRunningError("requested task is already running")

    # Create log entry now, so that future requests won't:  no task_id yet....
    tasklog_args = {'course_id': course_id,
                    'task_type': task_type,
                    'task_key': task_key,
                    'task_input': json.dumps(task_input),
                    'task_state': 'QUEUING',
                    'requester': requester}

    course_task_log = CourseTaskLog.objects.create(**tasklog_args)
    return course_task_log


@transaction.autocommit
def _update_task(course_task_log, task_result):
    """
    Updates a database entry with information about the submitted task.

    Autocommit annotation makes sure the database entry is committed.
    """
    # we at least update the entry with the task_id, and for EAGER mode,
    # we update other status as well.  (For non-EAGER modes, the entry
    # should not have changed except for setting PENDING state and the
    # addition of the task_id.)
    _update_course_task_log(course_task_log, task_result)
    course_task_log.save()


def _get_xmodule_instance_args(request):
    """
    Calculate parameters needed for instantiating xmodule instances.

    The `request_info` will be passed to a tracking log function, to provide information
    about the source of the task request.   The `xqueue_callback_urul_prefix` is used to
    permit old-style xqueue callbacks directly to the appropriate module in the LMS.
    """
    request_info = {'username': request.user.username,
                    'ip': request.META['REMOTE_ADDR'],
                    'agent': request.META.get('HTTP_USER_AGENT', ''),
                    'host': request.META['SERVER_NAME'],
                    }

    xmodule_instance_args = {'xqueue_callback_url_prefix': get_xqueue_callback_url_prefix(request),
                             'request_info': request_info,
                             }
    return xmodule_instance_args


def _update_course_task_log(course_task_log_entry, task_result):
    """
    Updates and possibly saves a CourseTaskLog entry based on a task Result.

    Used when a task initially returns, as well as when updated status is
    requested.

    Calculates json to store in task_progress field.
    """
    # Just pull values out of the result object once.  If we check them later,
    # the state and result may have changed.
    task_id = task_result.task_id
    result_state = task_result.state
    returned_result = task_result.result
    result_traceback = task_result.traceback

    # Assume we don't always update the CourseTaskLog entry if we don't have to:
    entry_needs_saving = False
    output = {}

    if result_state == 'PROGRESS':
        # construct a status message directly from the task result's result:
        if hasattr(task_result, 'result') and 'attempted' in returned_result:
            fmt = "Attempted {attempted} of {total}, {action_name} {updated}"
            message = fmt.format(attempted=returned_result['attempted'],
                                 updated=returned_result['updated'],
                                 total=returned_result['total'],
                                 action_name=returned_result['action_name'])
            output['message'] = message
            log.info("task progress: %s", message)
        else:
            log.info("still making progress... ")
        output['task_progress'] = returned_result

    elif result_state == 'SUCCESS':
        # save progress into the entry, even if it's not being saved here -- for EAGER,
        # it needs to go back with the entry passed in.
        course_task_log_entry.task_output = json.dumps(returned_result)
        output['task_progress'] = returned_result
        log.info("task succeeded: %s", returned_result)

    elif result_state == 'FAILURE':
        # on failure, the result's result contains the exception that caused the failure
        exception = returned_result
        traceback = result_traceback if result_traceback is not None else ''
        task_progress = {'exception': type(exception).__name__, 'message': str(exception.message)}
        output['message'] = exception.message
        log.warning("background task (%s) failed: %s %s", task_id, returned_result, traceback)
        if result_traceback is not None:
            output['task_traceback'] = result_traceback
            task_progress['traceback'] = result_traceback
        # save progress into the entry, even if it's not being saved -- for EAGER,
        # it needs to go back with the entry passed in.
        course_task_log_entry.task_output = json.dumps(task_progress)
        output['task_progress'] = task_progress

    elif result_state == 'REVOKED':
        # on revocation, the result's result doesn't contain anything
        # but we cannot rely on the worker thread to set this status,
        # so we set it here.
        entry_needs_saving = True
        message = 'Task revoked before running'
        output['message'] = message
        log.warning("background task (%s) revoked.", task_id)
        task_progress = {'message': message}
        course_task_log_entry.task_output = json.dumps(task_progress)
        output['task_progress'] = task_progress

    # always update the entry if the state has changed:
    if result_state != course_task_log_entry.task_state:
        course_task_log_entry.task_state = result_state
        course_task_log_entry.task_id = task_id

    if entry_needs_saving:
        course_task_log_entry.save()

    return output


def _get_course_task_log_status(task_id):
    """
    Get the status for a given task_id.

    Returns a dict, with the following keys:
      'task_id'
      'task_state'
      'in_progress': boolean indicating if the task is still running.
      'message': status message reporting on progress, or providing exception message if failed.
      'task_progress': dict containing progress information.  This includes:
          'attempted': number of attempts made
          'updated': number of attempts that "succeeded"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
          'duration_ms': how long the task has (or had) been running.
      'task_traceback': optional, returned if task failed and produced a traceback.
      'succeeded': on complete tasks, indicates if the task outcome was successful:
          did it achieve what it set out to do.
          This is in contrast with a successful task_state, which indicates that the
          task merely completed.

      If task doesn't exist, returns None.
    """
    # First check if the task_id is known
    try:
        course_task_log_entry = CourseTaskLog.objects.get(task_id=task_id)
    except CourseTaskLog.DoesNotExist:
        # TODO: log a message here
        return None

    # define ajax return value:
    status = {}

    # if the task is not already known to be done, then we need to query
    # the underlying task's result object:
    if course_task_log_entry.task_state not in READY_STATES:
        result = AsyncResult(task_id)
        status.update(_update_course_task_log(course_task_log_entry, result))
    elif course_task_log_entry.task_output is not None:
        # task is already known to have finished, but report on its status:
        status['task_progress'] = json.loads(course_task_log_entry.task_output)

    # status basic information matching what's stored in CourseTaskLog:
    status['task_id'] = course_task_log_entry.task_id
    status['task_state'] = course_task_log_entry.task_state
    status['in_progress'] = course_task_log_entry.task_state not in READY_STATES

    if course_task_log_entry.task_state in READY_STATES:
        succeeded, message = get_task_completion_message(course_task_log_entry)
        status['message'] = message
        status['succeeded'] = succeeded

    return status


def get_task_completion_message(course_task_log_entry):
    """
    Construct progress message from progress information in CourseTaskLog entry.

    Returns (boolean, message string) duple.

    Used for providing messages to course_task_log_status(), as well as
    external calls for providing course task submission history information.
    """
    succeeded = False

    if course_task_log_entry.task_output is None:
        log.warning("No task_output information found for course_task {0}".format(course_task_log_entry.task_id))
        return (succeeded, "No status information available")

    task_output = json.loads(course_task_log_entry.task_output)
    if course_task_log_entry.task_state in ['FAILURE', 'REVOKED']:
        return(succeeded, task_output['message'])

    action_name = task_output['action_name']
    num_attempted = task_output['attempted']
    num_updated = task_output['updated']
    num_total = task_output['total']

    if course_task_log_entry.task_input is None:
        log.warning("No task_input information found for course_task {0}".format(course_task_log_entry.task_id))
        return (succeeded, "No status information available")
    task_input = json.loads(course_task_log_entry.task_input)
    problem_url = task_input.get('problem_url', None)
    student = task_input.get('student', None)
    if student is not None:
        if num_attempted == 0:
            msg = "Unable to find submission to be {action} for student '{student}'"
        elif num_updated == 0:
            msg = "Problem failed to be {action} for student '{student}'"
        else:
            succeeded = True
            msg = "Problem successfully {action} for student '{student}'"
    elif num_attempted == 0:
        msg = "Unable to find any students with submissions to be {action}"
    elif num_updated == 0:
        msg = "Problem failed to be {action} for any of {attempted} students"
    elif num_updated == num_attempted:
        succeeded = True
        msg = "Problem successfully {action} for {attempted} students"
    elif num_updated < num_attempted:
        msg = "Problem {action} for {updated} of {attempted} students"

    if student is not None and num_attempted != num_total:
        msg += " (out of {total})"

    # Update status in task result object itself:
    message = msg.format(action=action_name, updated=num_updated, attempted=num_attempted, total=num_total,
                         student=student, problem=problem_url)
    return (succeeded, message)


########### Add task-submission methods here:

def _check_arguments_for_rescoring(course_id, problem_url):
    """
    Do simple checks on the descriptor to confirm that it supports rescoring.

    Confirms first that the problem_url is defined (since that's currently typed
    in).  An ItemNotFoundException is raised if the corresponding module
    descriptor doesn't exist.  NotImplementedError is returned if the
    corresponding module doesn't support rescoring calls.
    """
    descriptor = modulestore().get_instance(course_id, problem_url)
    supports_rescore = False
    if hasattr(descriptor, 'module_class'):
        module_class = descriptor.module_class
        if hasattr(module_class, 'rescore_problem'):
            supports_rescore = True

    if not supports_rescore:
        msg = "Specified module does not support rescoring."
        raise NotImplementedError(msg)


def _encode_problem_and_student_input(problem_url, student=None):
    """
    Encode problem_url and optional student into task_key and task_input values.

    `problem_url` is full URL of the problem.
    `student` is the user object of the student
    """
    if student is not None:
        task_input = {'problem_url': problem_url, 'student': student.username}
        task_key = "{student}_{problem}".format(student=student.id, problem=problem_url)
    else:
        task_input = {'problem_url': problem_url}
        task_key = "{student}_{problem}".format(student="", problem=problem_url)

    return task_input, task_key


def _submit_task(request, task_type, task_class, course_id, task_input, task_key):
    """
    """
    # check to see if task is already running, and reserve it otherwise:
    course_task_log = _reserve_task(course_id, task_type, task_key, task_input, request.user)

    # submit task:
    task_args = [course_task_log.id, course_id, task_input, _get_xmodule_instance_args(request)]
    task_result = task_class.apply_async(task_args)

    # Update info in table with the resulting task_id (and state).
    _update_task(course_task_log, task_result)

    return course_task_log


def submit_rescore_problem_for_student(request, course_id, problem_url, student):
    """
    Request a problem to be rescored as a background task.

    The problem will be rescored for the specified student only.  Parameters are the `course_id`,
    the `problem_url`, and the `student` as a User object.
    The url must specify the location of the problem, using i4x-type notation.

    An exception is thrown if the problem doesn't exist, or if the particular
    problem is already being rescored for this student.
    """
    # check arguments:  let exceptions return up to the caller.
    _check_arguments_for_rescoring(course_id, problem_url)

    task_type = 'rescore_problem'
    task_class = rescore_problem
    task_input, task_key = _encode_problem_and_student_input(problem_url, student)
    return _submit_task(request, task_type, task_class, course_id, task_input, task_key)


def submit_rescore_problem_for_all_students(request, course_id, problem_url):
    """
    Request a problem to be rescored as a background task.

    The problem will be rescored for all students who have accessed the
    particular problem in a course and have provided and checked an answer.
    Parameters are the `course_id` and the `problem_url`.
    The url must specify the location of the problem, using i4x-type notation.

    An exception is thrown if the problem doesn't exist, or if the particular
    problem is already being rescored.
    """
    # check arguments:  let exceptions return up to the caller.
    _check_arguments_for_rescoring(course_id, problem_url)

    # check to see if task is already running, and reserve it otherwise
    task_type = 'rescore_problem'
    task_class = rescore_problem
    task_input, task_key = _encode_problem_and_student_input(problem_url)
    return _submit_task(request, task_type, task_class, course_id, task_input, task_key)


def submit_reset_problem_attempts_for_all_students(request, course_id, problem_url):
    """
    Request to have attempts reset for a problem as a background task.

    The problem's attempts will be reset for all students who have accessed the
    particular problem in a course.  Parameters are the `course_id` and
    the `problem_url`.  The url must specify the location of the problem,
    using i4x-type notation.

    An exception is thrown if the problem doesn't exist, or if the particular
    problem is already being reset.
    """
    # check arguments:  make sure that the problem_url is defined
    # (since that's currently typed in).  If the corresponding module descriptor doesn't exist,
    # an exception will be raised.  Let it pass up to the caller.
    modulestore().get_instance(course_id, problem_url)

    task_type = 'reset_problem_attempts'
    task_class = reset_problem_attempts
    task_input, task_key = _encode_problem_and_student_input(problem_url)
    return _submit_task(request, task_type, task_class, course_id, task_input, task_key)


def submit_delete_problem_state_for_all_students(request, course_id, problem_url):
    """
    Request to have state deleted for a problem as a background task.

    The problem's state will be deleted for all students who have accessed the
    particular problem in a course.  Parameters are the `course_id` and
    the `problem_url`.  The url must specify the location of the problem,
    using i4x-type notation.

    An exception is thrown if the problem doesn't exist, or if the particular
    problem is already being deleted.
    """
    # check arguments:  make sure that the problem_url is defined
    # (since that's currently typed in).  If the corresponding module descriptor doesn't exist,
    # an exception will be raised.  Let it pass up to the caller.
    modulestore().get_instance(course_id, problem_url)

    task_type = 'delete_problem_state'
    task_class = delete_problem_state
    task_input, task_key = _encode_problem_and_student_input(problem_url)
    return _submit_task(request, task_type, task_class, course_id, task_input, task_key)
