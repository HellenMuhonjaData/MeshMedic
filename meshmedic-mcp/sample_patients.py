"""
Sample patient identity data for MeshMedic's search_ehr_patient tool.

No real patient data exists in this project yet, so this is a small, clearly
fictional stand-in shaped like what the real EHR-agnostic search (Epic /
Oracle Health) would return: MRN, name, date of birth, source system, and
the internal patient_id that a later patient-chart resource would resolve to.

Includes two people sharing a last name (Whitfield) with different dates of
birth, so last_name + date_of_birth searches have a realistic disambiguation
case to exercise.
"""

SAMPLE_PATIENTS = [
    {
        "mrn": "E10293847",
        "first_name": "Grace",
        "last_name": "Whitfield",
        "date_of_birth": "1968-03-14",
        "ehr_system": "epic",
        "patient_id": "epic-pt-10293847",
    },
    {
        "mrn": "O88213",
        "first_name": "Marcus",
        "last_name": "Delgado",
        "date_of_birth": "1990-11-02",
        "ehr_system": "oracle_health",
        "patient_id": "oh-pt-88213",
    },
    {
        "mrn": "E55219",
        "first_name": "Priya",
        "last_name": "Natarajan",
        "date_of_birth": "1975-07-22",
        "ehr_system": "epic",
        "patient_id": "epic-pt-55219",
    },
    {
        "mrn": "O30044",
        "first_name": "James",
        "last_name": "Whitfield",
        "date_of_birth": "1954-01-09",
        "ehr_system": "oracle_health",
        "patient_id": "oh-pt-30044",
    },
]
