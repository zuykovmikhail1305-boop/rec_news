from dataclasses import dataclass

@dataclass(frozen=True)
class Keywords:
    pass


@dataclass(frozen=True)
class News:
    dense_vector: list[float]
    time_published: str
    keywords: Keywords
    type_event: str

@dataclass(frozen=True)
class Centroid:
    dense_vector: list[float]
    last_updated: str
    news_count: int
    


class Connect:
    def __init__(self):
        pass