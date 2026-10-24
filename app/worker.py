import time
import hashlib
import feedparser
from datetime import datetime
from rapidfuzz import fuzz
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, SourceConfigModel, NewsRawModel

# Подключение к базе (для MVP используем SQLite, для финала легко меняется на PostgreSQL)
DATABASE_URL = "sqlite:///alfa.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    # Создаем таблицы, если их еще нет
    Base.metadata.create_all(bind=engine)
    
    # Добавим тестовый источник, если таблица пуста
    session = SessionLocal()
    if session.query(SourceConfigModel).count() == 0:
        default_source = SourceConfigModel(
            name="Ведомости RSS", 
            url="https://www.vedomosti.ru/rss/news", 
            source_type="rss"
        )
        session.add(default_source)
        session.commit()
    session.close()

class SQLAlchemyCollector:
    def __init__(self):
        self.keywords_hint = ["ставка", "цб", "кредит", "лизинг", "инфляция", "валюта", "налог"]

    def process_rss_sources(self):
        session = SessionLocal()
        sources = session.query(SourceConfigModel).filter_by(source_type="rss").all()
        
        for source in sources:
            print(f"Опрос RSS: {source.name}")
            try:
                feed = feedparser.parse(source.url)
                for entry in feed.entries[:10]:
                    self._save_entry(session, source.id, entry)
                time.sleep(3) # Rate-limit
            except Exception as e:
                print(f"Ошибка при опросе {source.name}: {e}")
                
        session.close()

    def _save_entry(self, session, source_id, entry):
        title = entry.get("title", "").strip()
        url = entry.get("link", "").strip()
        text = entry.get("summary", "") or entry.get("description", "")
        published = entry.get("published", str(datetime.now()))

        if not title or not url:
            return

        # 1. Точная дедупликация по SHA-256 хэшу заголовка
        title_hash = hashlib.sha256(title.encode('utf-8')).hexdigest()
        existing_by_hash = session.query(NewsRawModel).filter_by(title_hash=title_hash).first()
        if existing_by_hash:
            return

        # 2. Нечеткая дедупликация через rapidfuzz (проверяем последние 100 записей)
        recent_news = session.query(NewsRawModel).order_by(NewsRawModel.id.desc()).limit(100).all()
        for item in recent_news:
            if fuzz.ratio(title, item.title) >= 90:
                return # Дубликат обнаружен

        # 3. Эвристический префильтр (без LLM)
        full_content = (title + " " + text).lower()
        relevance_hint = any(kw in full_content for kw in self.keywords_hint)

        # 4. Сохранение в БД через SQLAlchemy
        try:
            new_news = NewsRawModel(
                source_id=source_id,
                title=title,
                text=text,
                published_at=published,
                url=url,
                title_hash=title_hash,
                relevance_hint=relevance_hint
            )
            session.add(new_news)
            session.commit()
            print(f"[УСПЕХ] Сохранено: {title} (hint: {int(relevance_hint)})")
        except Exception as e:
            session.rollback()
            # Срабатывает при дубликате по уникальному URL
            pass

if __name__ == "__main__":
    init_db()
    collector = SQLAlchemyCollector()
    collector.process_rss_sources()