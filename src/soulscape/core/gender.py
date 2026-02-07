class Gender:
    current_gender_id: int = 0

    def __init__(self, gender_name: str, can_give_birth: bool):
        Gender.current_gender_id += 1
        self.gender_id: int = Gender.current_gender_id
        self.gender_name: str = gender_name
        self.can_give_birth: bool = can_give_birth

    def __repr__(self):
        return self.gender_name

    def __eq__(self, other):
        if isinstance(other, str):
            return self.gender_name == other
        return super().__eq__(other)
