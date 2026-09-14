from pydantic import BaseModel, PositiveInt
from .enums import Stat


class Gender(BaseModel):
    gender_id: PositiveInt
    gender_name: str
    can_give_birth: bool


class Ability(BaseModel):
    ability_id: PositiveInt
    name: str
    description: str


class Nature(BaseModel):
    nature_id: PositiveInt
    nature_name: str
    stat_modifiers: dict[Stat, float]


class BaseStats(BaseModel):
    hp: PositiveInt
    attack: PositiveInt
    defense: PositiveInt
    sp_atk: PositiveInt
    sp_def: PositiveInt
    speed: PositiveInt
    vision: PositiveInt


class IvStats(BaseModel):
    hp: PositiveInt
    attack: PositiveInt
    defense: PositiveInt
    sp_atk: PositiveInt
    sp_def: PositiveInt
    speed: PositiveInt
    vision: PositiveInt


class EvStats(BaseModel):
    hp: PositiveInt
    attack: PositiveInt
    defense: PositiveInt
    sp_atk: PositiveInt
    sp_def: PositiveInt
    speed: PositiveInt
    vision: PositiveInt


class Evolution(BaseModel):
    level: PositiveInt
    evolves_to: "Species"


class Rarity(BaseModel):
    rarity_id: PositiveInt
    rarity_name: str
    spawn_rate: float


class Species(BaseModel):
    species_id: PositiveInt
    name: str
    rarity: Rarity
    genders: list[Gender]
    abilities: list[Ability]
    base_stats: BaseStats
    evolutions: list[Evolution] | None = None


class Soul(BaseModel):
    soul_id: str
    name: str
    species: Species
    gender: Gender
    base_stats: BaseStats
    iv_stats: IvStats
    ev_stats: EvStats
    nature: Nature
    ability: Ability
    level: PositiveInt
    current_health: PositiveInt


class Tamer(BaseModel):
    tamer_id: PositiveInt
    name: str
    captured_souls: list[Soul]
