"""Fixed, identity-free rejection codes. Never include input values in errors."""

ERROR_CODES = frozenset(
    """aircraft_limit airport_config archive_size body_date configuration conflicting_duplicate
    day_argument duplicate_airport duplicate_json_key identity_format json_complexity json_size
    member_limit member_size method_version no_traces opt_out_config operator_config output_is_input
    output_limit part_count part_hash part_length part_names path_date piece_limit point_altitude
    point_date point_details point_order point_position point_schema rejected tar_directory_size
    tar_end_markers tar_header tar_member_type tar_padding tar_trailing_data trace_encoding
    trace_identity trace_path_identity trace_schema trace_version track_limit truncated_tar
    unexpected_tar_member unsafe_tar_path""".split()
)


def rejection_code(code):
    return code if isinstance(code, str) and code in ERROR_CODES else "rejected"


class DataRejected(ValueError):
    def __init__(self, code: str, *, date_error: bool = False):
        self.code = rejection_code(code)
        self.date_error = date_error
        super().__init__(self.code)
