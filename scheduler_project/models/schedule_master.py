class ScheduleMaster:
    """
    Plain Python representation of a Schedule Master job.

    This is intentionally NOT a Django ORM model.
    """

    def __init__(
        self,
        id=None,
        name=None,
        package_name=None,
        run_config=None,
        margin=None,
        same_day=0,
        time_flag=0,
        is_active=1,
        created_date=None,
        confirmation_needed=None,
    ):
        self.id = id
        self.name = name
        self.package_name = package_name
        self.run_config = run_config
        self.margin = margin
        self.same_day = same_day
        self.time_flag = time_flag
        self.is_active = is_active
        self.created_date = created_date
        self.confirmation_needed = confirmation_needed

    def __repr__(self):
        return (
            f"ScheduleMaster("
            f"id={self.id}, "
            f"name={self.name!r}, "
            f"margin={self.margin!r}, "
            f"same_day={self.same_day}, "
            f"time_flag={self.time_flag}, "
            f"is_active={self.is_active}"
            f")"
        )