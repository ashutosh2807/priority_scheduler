class ConfirmationEvaluator:
    """
    Handles the CONFIRMATION_NEEDED rule.

    Confirmation is an eligibility gate.

    It does NOT:
        - change job priority
        - execute the job
        - modify Schedule Master
        - modify DATEMAST

    Flow:

        CONFIRMATION_NEEDED = 0
                |
                v
             proceed

        CONFIRMATION_NEEDED = 1
                |
                v
        confirmation = 0
                |
                v
        WAITING_CONFIRMATION

        confirmation = 1
                |
                v
             proceed
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    CONFIRMED = "CONFIRMED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"

    # =========================================================
    # Is confirmation required?
    # =========================================================

    def is_required(self, job):
        """
        Determine whether the job requires user confirmation.
        """

        value = getattr(
            job,
            "confirmation_needed",
            None,
        )

        return self._to_bool(
            value
        )

    # =========================================================
    # Is confirmation present?
    # =========================================================

    def is_confirmed(self, control):
        """
        Determine whether the job has been confirmed through
        the authoritative job_control state.
        """

        if not control:
            return False

        value = control.get(
            "confirmation",
            0,
        )

        return self._to_bool(
            value
        )

    # =========================================================
    # Get state
    # =========================================================

    def get_state(
        self,
        job,
        control,
    ):
        """
        Return the current confirmation state.
        """

        if not self.is_required(
            job
        ):
            return self.NOT_REQUIRED

        if self.is_confirmed(
            control
        ):
            return self.CONFIRMED

        return self.WAITING_CONFIRMATION

    # =========================================================
    # Can proceed?
    # =========================================================

    def can_proceed(
        self,
        job,
        control,
    ):
        """
        Return True if the confirmation gate has passed.
        """

        state = self.get_state(
            job,
            control,
        )

        return state in {
            self.NOT_REQUIRED,
            self.CONFIRMED,
        }

    # =========================================================
    # Explanation
    # =========================================================

    def get_reason(
        self,
        job,
        control,
    ):
        """
        Return a human-readable explanation suitable for the
        staging table / monitoring UI.
        """

        state = self.get_state(
            job,
            control,
        )

        if state == self.NOT_REQUIRED:
            return (
                "Confirmation not required."
            )

        if state == self.CONFIRMED:
            return (
                "Job confirmed."
            )

        return (
            "Waiting for user confirmation."
        )

    # =========================================================
    # Convert value to bool
    # =========================================================

    @staticmethod
    def _to_bool(value):
        """
        Convert Oracle/SQLite/config style values into bool.

        Supported examples:

            1
            0
            True
            False
            "1"
            "0"
            "Y"
            "N"
            "YES"
            "NO"
            "TRUE"
            "FALSE"
        """

        if value is None:
            return False

        if isinstance(
            value,
            bool,
        ):
            return value

        if isinstance(
            value,
            int,
        ):
            return value != 0

        if isinstance(
            value,
            str,
        ):
            value = value.strip().upper()

            if value in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }:
                return True

            if value in {
                "0",
                "N",
                "NO",
                "FALSE",
                "F",
                "",
            }:
                return False

        return bool(value)