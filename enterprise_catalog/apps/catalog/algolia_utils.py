import copy
import logging

from enterprise_catalog.apps.api.v1.utils import is_course_run_active
from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient


logger = logging.getLogger(__name__)

ALGOLIA_UUID_BATCH_SIZE = 100


# keep attributes from course objects that we explicitly want in Algolia
ALGOLIA_FIELDS = [
    'additional_information',
    'availability',
    'card_image_url',  # for display on course cards
    'enterprise_catalog_uuids',
    'enterprise_catalog_query_uuids',
    'enterprise_customer_uuids',
    'full_description',
    'key',  # for links to Course about pages from the Learner Portal search page
    'language',
    'level_type',
    'objectID',  # required by Algolia, e.g. "course-{uuid}"
    'partners',
    'programs',
    'program_titles',
    'recent_enrollment_count',
    'short_description',
    'subjects',
    'skill_names',
    'skills',
    'title',
    'advertised_course_run',  # a part of the advertised course run
]

# default configuration for the index
ALGOLIA_INDEX_SETTINGS = {
    'attributeForDistinct': 'key',
    'distinct': True,
    'typoTolerance': False,
    'searchableAttributes': [
        'unordered(title)',
        'unordered(full_description)',
        'unordered(short_description)',
        'unordered(additional_information)',
        'partners',
        'skill_names',
        'skills',
    ],
    'attributesForFaceting': [
        'availability',
        'enterprise_catalog_uuids',
        'enterprise_catalog_query_uuids',
        'enterprise_customer_uuids',
        'language',
        'level_type',
        'searchable(partners.name)',
        'searchable(programs)',
        'searchable(program_titles)',
        'searchable(skill_names)',
        'searchable(skills)',
        'searchable(subjects)',
    ],
    'unretrievableAttributes': [
        'enterprise_catalog_uuids',
        'enterprise_catalog_query_uuids',
        'enterprise_customer_uuids',
    ],
    'customRanking': [
        'desc(recent_enrollment_count)',
    ],
}


def _should_index_course(course_metadata):
    """
    Replicates the B2C index check of whether a certain course should be indexed for search.

    A course should only be indexed for algolia search if it has a non-indexed advertiseable course run, at least
    one owner, and a marketing url slug.
    The course-discovery check that the course's partner is edX is included by default as the discovery API filters
    to the request's site's partner.
    The discovery check that the course has an availability level was decided to be a duplicate check that the
    website team plans on removing.
    Original code:
    https://github.com/edx/course-discovery/blob/c6ac5329225e2f32cdf1d1da855d7c9d905b2576/course_discovery/apps/course_metadata/algolia_models.py#L218-L227

    Args:
        course (ContentMetadata): The ContentMetadata representing a course object.

    Returns:
        bool: Whether or not the course should be indexed by algolia.
    """
    course_json_metadata = course_metadata.json_metadata
    advertised_course_run_uuid = course_json_metadata.get('advertised_course_run_uuid')
    advertised_course_run = _get_course_run_by_uuid(
        course_json_metadata,
        advertised_course_run_uuid,
    )

    if advertised_course_run is None:
        return False

    if not is_course_run_active(advertised_course_run):
        return False

    owners = course_json_metadata.get('owners') or []
    return not advertised_course_run.get('hidden') and len(owners) > 0


def partition_course_keys_for_indexing(courses_content_metadata):
    """
    Returns both the indexable and non-indexable course content keys for Algolia.

    Args:
        courses_content_metadata (list of ContentMetadata): A list of ContentMetadata objects representing courses that
            should be filtered down.

    Returns:
        indexable_course_keys (list): Content key strings to be indexed
        nonindexable_course_keys (list): Content key strings to NOT be indexed
    """
    indexable_course_keys = set()
    nonindexable_course_keys = set()

    for course_metadata in courses_content_metadata:
        if _should_index_course(course_metadata):
            indexable_course_keys.add(course_metadata.content_key)
        else:
            nonindexable_course_keys.add(course_metadata.content_key)

    return list(indexable_course_keys), list(nonindexable_course_keys)


def get_initialized_algolia_client():
    """
    Initializes and returns an Algolia client for updating search indices
    """
    algolia_client = AlgoliaSearchClient()
    algolia_client.init_index()
    return algolia_client


def configure_algolia_index(algolia_client):
    """
    Configures the settings for an Algolia index.
    """
    algolia_client.set_index_settings(ALGOLIA_INDEX_SETTINGS)


def get_algolia_object_id(uuid):
    """
    Given a uuid, returns an object_id to use for Algolia indexing.

    Arguments:
        uuid (str): a course uuid

    Returns:
        str: the generated Algolia object_id or None if uuid is not specified
    """
    if uuid:
        return f'course-{uuid}'
    return None


def get_course_language(course):
    """
    Gets the human-readable language name associated with the advertised course run. Used for
    the "Language" facet in Algolia.

    Arguments:
        course (dict): a dict representing with course metadata

    Returns:
        string: human-readable language name parsed from a language code, or None if language name is not present.
    """
    advertised_course_run = _get_course_run_by_uuid(course, course.get('advertised_course_run_uuid'))
    if not advertised_course_run:
        return None

    content_language_name = advertised_course_run.get('content_language_search_facet_name')
    return content_language_name


def get_course_availability(course):
    """
    Gets the availability for a course. Used for the "Availability" facet in Algolia.

    Arguments:
        course (dict): a dict representing with course metadata

    Returns:
        list: a list of availabilities for those course runs (e.g., "Upcoming")
    """
    DEFAULT_COURSE_AVAILABILITY = 'Archived'
    COURSE_AVAILABILITY_MESSAGES = {
        'current': 'Available Now',
        'upcoming': 'Upcoming',
    }
    course_runs = course.get('course_runs') or []
    active_course_runs = [run for run in course_runs if is_course_run_active(run)]
    availability = set()
    for course_run in active_course_runs:
        run_availability = course_run.get('availability') or ''
        availability.add(
            COURSE_AVAILABILITY_MESSAGES.get(run_availability.lower(), DEFAULT_COURSE_AVAILABILITY)
        )
    return list(availability)


def get_course_partners(course):
    """
    Gets list of partners associated with the course. Used for the "Partners" facet and
    searchable attribute in Algolia.

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        list: a list of partner metadata associated with the course
    """
    partners = []
    owners = course.get('owners') or []

    for owner in owners:
        partner_name = owner.get('name')
        if partner_name:
            partner_metadata = {
                'name': partner_name,
                'logo_image_url': owner.get('logo_image_url'),
            }
            partners.append(partner_metadata)

    return partners


def _get_course_program_field(course, field):
    """
    Helper to pluck a list of values for the given field out of a course's programs.

    Arguments:
        course (dict): a dictionary representing a course
        field (str): the name of a field to return values of.
    Returns:
        list: a list of the values for a certain field in a program associated with the course.
    """
    programs = course.get('programs') or []
    return list({
        value for program in programs
        if (value := program.get(field))
    })


def get_course_program_types(course):
    """
    Gets list of program types associated with the course. Used for the "Programs"
    facet in Algolia.

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        list: a list of program types associated with the course
    """
    return _get_course_program_field(course, 'type')


def get_course_program_titles(course):
    """
    Gets list of program titles associated with the course. Used for the "Program titles"
    facet in Algolia.

    Arguments:
        course (dict): a dictionary representing a course.

    Returns:
        list: a list of program titles associated with the course.
    """
    return _get_course_program_field(course, 'title')


def get_course_subjects(course):
    """
    Gets list of subject names associated with the course. Used for the "Subjects"
    facet in Algolia.

    `course.get('subjects')` may be either:
        - a list of strings, e.g. ['Communication']
        - a list of dictionaries, e.g. [{'name': 'Communication'}]

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        list: a list of subject names associated with the course
    """
    subject_names = set()
    subjects = course.get('subjects') or []

    for subject in subjects:
        if isinstance(subject, str):
            subject_names.add(subject)
            continue

        subject_name = subject.get('name')
        if subject_name:
            subject_names.add(subject_name)

    return list(subject_names)


def get_course_card_image_url(course):
    """
    Gets the appropriate image to use for course cards.

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        str: the url for the course card image
    """
    image_url = course.get('image_url')
    return image_url


def get_course_skill_names(course):
    """
    Gets the skill names associated with a course.

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        list: a list of skill names associated with the course
    """
    skill_names = course.get('skill_names') or []
    return list(set(skill_names))


def get_course_skills(course):
    """
    Gets the skills associated with a course.

    Arguments:
        course (dict): a dictionary representing a course

    Returns:
        skills (list): list of dictionaries containing skill name, description
    """
    skills = course.get('skills') or []
    return list(skills)


def get_advertised_course_run(course):
    """
    Get part of the advertised course_run as per advertised_course_run_uuid

    Argument:
        course (dict)

    Returns:
        dict: containing key, pacing_type, start and end for the course_run, or None
    """
    full_course_run = _get_course_run_by_uuid(course, course.get('advertised_course_run_uuid'))
    if full_course_run is None:
        return None
    course_run = {
        'key': full_course_run.get('key'),
        'pacing_type': full_course_run.get('pacing_type'),
        'start': full_course_run.get('start'),
        'end': full_course_run.get('end'),
    }
    return course_run


def _get_course_run_by_uuid(course, course_run_uuid):
    """
    Find a course_run based on uuid

    Arguments:
        course (dict): course dict
        course_run_uuid (str): uuid to lookup

    Returns:
        dict: a course_run or None
    """
    try:
        course_run = [run for run in course.get('course_runs', []) if run.get('uuid') == course_run_uuid][0]
    except IndexError:
        return None
    return course_run


def _algolia_object_from_course(course, algolia_fields):
    """
    Transforms a course into an Algolia object.

    Arguments:
        course (dict): a course dict
        algolia_fields (list): list of fields to extract from the course

    Returns:
        dict: a dictionary containing only the fields noted in algolia_fields
    """
    searchable_course = copy.deepcopy(course)
    searchable_course.update({
        'language': get_course_language(searchable_course),
        'availability': get_course_availability(searchable_course),
        'partners': get_course_partners(searchable_course),
        'programs': get_course_program_types(searchable_course),
        'program_titles': get_course_program_titles(searchable_course),
        'subjects': get_course_subjects(searchable_course),
        'card_image_url': get_course_card_image_url(searchable_course),
        'advertised_course_run': get_advertised_course_run(searchable_course),
        'skill_names': get_course_skill_names(searchable_course),
        'skills': get_course_skills(searchable_course),
    })

    algolia_object = {}
    for field in algolia_fields:
        field_value = searchable_course.get(field)
        if field_value is not None:
            algolia_object[field] = field_value

    return algolia_object


def create_algolia_objects_from_courses(courses, algolia_fields):
    """
    Transforms all courses into Algolia objects.

    Arguments:
        courses (list): list of courses
        algolia_fields (list): list of fields to extract from courses

    Returns:
        list: a list of Algolia objects containing only the fields noted in algolia_fields
    """
    if not algolia_fields:
        algolia_fields = []

    algolia_objects = [
        _algolia_object_from_course(course, algolia_fields)
        for course in courses
    ]

    return algolia_objects
