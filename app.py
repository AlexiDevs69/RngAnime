from __future__ import annotations

import base64
import hashlib
import hmac
import io
import math
import os
import random
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Generator

import cloudinary
import cloudinary.uploader
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    joinedload,
    mapped_column,
    relationship,
    sessionmaker,
)


ROOT = Path(__file__).resolve().parent
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rift_roll.db").strip()
if RAW_DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = RAW_DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif RAW_DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = RAW_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = RAW_DATABASE_URL

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-before-production")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true" if os.getenv("RENDER") else "false").lower() == "true"
SESSION_COOKIE = "rift_roll_session"
SESSION_DAYS = 30
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RNG = random.SystemRandom()
Image.MAX_IMAGE_PIXELS = 40_000_000
if CLOUDINARY_URL:
    cloudinary.config(secure=True)


engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


card_mutations = Table(
    "card_mutations",
    Base.metadata,
    Column("card_id", ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True),
    Column("mutation_id", ForeignKey("mutations.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(32))
    handle: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    bio: Mapped[str] = mapped_column(String(190), default="Ще не залишив опис профілю.")
    accent: Mapped[str] = mapped_column(String(16), default="#5865f2")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    coins: Mapped[int] = mapped_column(BigInteger, default=2500)
    rolls: Mapped[int] = mapped_column(Integer, default=0)
    last_roll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_income_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())


class GameEvent(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    accent: Mapped[str] = mapped_column(String(16), default="#f0b232")
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    subtitle: Mapped[str] = mapped_column(String(160), default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    rarity_name: Mapped[str] = mapped_column(String(32), default="Common")
    rarity_tier: Mapped[int] = mapped_column(Integer, default=0)
    rarity_color: Mapped[str] = mapped_column(String(16), default="#949ba4")
    base_weight: Mapped[float] = mapped_column(Float)
    income_per_second: Mapped[int] = mapped_column(BigInteger, default=1)
    event_only: Mapped[bool] = mapped_column(Boolean, default=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())

    event: Mapped[GameEvent | None] = relationship()
    mutations: Mapped[list[Mutation]] = relationship(secondary=card_mutations, back_populates="cards")


class Mutation(Base):
    __tablename__ = "mutations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    color: Mapped[str] = mapped_column(String(16), default="#ffffff")
    chance: Mapped[float] = mapped_column(Float)
    income_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    event_only: Mapped[bool] = mapped_column(Boolean, default=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())

    event: Mapped[GameEvent | None] = relationship()
    cards: Mapped[list[Card]] = relationship(secondary=card_mutations, back_populates="mutations")


class Potion(Base):
    __tablename__ = "potions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(220), default="")
    effect_type: Mapped[str] = mapped_column(String(16))
    multiplier: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    color: Mapped[str] = mapped_column(String(16), default="#5865f2")
    starter_quantity: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserPotion(Base):
    __tablename__ = "user_potions"
    __table_args__ = (UniqueConstraint("user_id", "potion_id", name="uq_user_potion"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    potion_id: Mapped[int] = mapped_column(ForeignKey("potions.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    potion: Mapped[Potion] = relationship()


class InventoryItem(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("user_id", "card_id", "mutation_key", name="uq_inventory_variant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    mutation_id: Mapped[int | None] = mapped_column(ForeignKey("mutations.id", ondelete="SET NULL"), nullable=True)
    mutation_key: Mapped[str] = mapped_column(String(32), default="base")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    first_obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())
    last_obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())

    card: Mapped[Card] = relationship()
    mutation: Mapped[Mutation | None] = relationship()


class RollHistory(Base):
    __tablename__ = "roll_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"))
    mutation_id: Mapped[int | None] = mapped_column(ForeignKey("mutations.id", ondelete="SET NULL"), nullable=True)
    effective_luck: Mapped[float] = mapped_column(Float, default=1.0)
    adjusted_chance: Mapped[float] = mapped_column(Float, default=0.0)
    rolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow(), index=True)

    user: Mapped[User] = relationship()
    card: Mapped[Card] = relationship()
    mutation: Mapped[Mutation | None] = relationship()


class ShowcaseSlot(Base):
    __tablename__ = "showcase_slots"
    __table_args__ = (
        UniqueConstraint("user_id", "page_index", "slot_index", name="uq_showcase_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    inventory_id: Mapped[int] = mapped_column(ForeignKey("inventory.id", ondelete="CASCADE"))
    page_index: Mapped[int] = mapped_column(Integer)
    slot_index: Mapped[int] = mapped_column(Integer)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())

    inventory: Mapped[InventoryItem] = relationship()


class Dice(Base):
    __tablename__ = "dice"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(220), default="")
    face_color: Mapped[str] = mapped_column(String(16), default="#f2f3f5")
    pip_color: Mapped[str] = mapped_column(String(16), default="#16171a")
    texture_url: Mapped[str] = mapped_column(Text, default="")
    base_luck: Mapped[float] = mapped_column(Float, default=1.0)
    unlock_cost: Mapped[int] = mapped_column(BigInteger, default=0)
    upgrade_base_cost: Mapped[int] = mapped_column(BigInteger, default=500)
    luck_growth: Mapped[float] = mapped_column(Float, default=1.35)
    max_level: Mapped[int] = mapped_column(Integer, default=10)
    required_level: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())


class UserDice(Base):
    __tablename__ = "user_dice"
    __table_args__ = (UniqueConstraint("user_id", "dice_id", name="uq_user_dice"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dice_id: Mapped[int] = mapped_column(ForeignKey("dice.id", ondelete="CASCADE"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    is_equipped: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())

    dice: Mapped[Dice] = relationship()


class UserAvatar(Base):
    __tablename__ = "user_avatars"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    avatar_url: Mapped[str] = mapped_column(Text)
    public_id: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow())


class PotionShopOffer(Base):
    __tablename__ = "potion_shop_offers"

    potion_id: Mapped[int] = mapped_column(
        ForeignKey("potions.id", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[int] = mapped_column(BigInteger, default=1000)
    pack_size: Mapped[int] = mapped_column(Integer, default=1)
    stock_limit: Mapped[int] = mapped_column(Integer, default=10)
    stock_remaining: Mapped[int] = mapped_column(Integer, default=10)
    restock_seconds: Mapped[int] = mapped_column(Integer, default=300)
    restocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: utcnow()
    )
    required_rebirths: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    potion: Mapped[Potion] = relationship()


class RebirthTier(Base):
    __tablename__ = "rebirth_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tier: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(240), default="")
    required_coins: Mapped[int] = mapped_column(BigInteger)
    luck_multiplier: Mapped[float] = mapped_column(Float, default=1.15)
    income_multiplier: Mapped[float] = mapped_column(Float, default=1.15)
    accent: Mapped[str] = mapped_column(String(16), default="#5865f2")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: utcnow()
    )

    requirements: Mapped[list[RebirthRequirement]] = relationship(
        back_populates="rebirth_tier",
        cascade="all, delete-orphan",
        order_by="RebirthRequirement.id",
    )


class RebirthRequirement(Base):
    __tablename__ = "rebirth_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    rebirth_tier_id: Mapped[int] = mapped_column(
        ForeignKey("rebirth_tiers.id", ondelete="CASCADE"), index=True
    )
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT"), index=True
    )
    mutation_id: Mapped[int | None] = mapped_column(
        ForeignKey("mutations.id", ondelete="RESTRICT"), nullable=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    rebirth_tier: Mapped[RebirthTier] = relationship(back_populates="requirements")
    card: Mapped[Card] = relationship()
    mutation: Mapped[Mutation | None] = relationship()


class UserRebirth(Base):
    __tablename__ = "user_rebirths"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    completed_tier: Mapped[int] = mapped_column(Integer, default=0)
    total_coins_spent: Mapped[int] = mapped_column(BigInteger, default=0)
    last_rebirth_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    value = aware(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def normalize_chance(value: float) -> float:
    if 1 < value <= 100:
        value /= 100
    return min(1.0, max(0.000001, value))


def clean_color(value: str, fallback: str) -> str:
    return value if COLOR_RE.fullmatch(value.strip()) else fallback


def required_admin_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{label} не може бути порожньою.")
    return cleaned


def validate_event_link(
    db: Session,
    *,
    event_only: bool,
    event_id: int | None,
    item_label: str,
) -> int | None:
    if event_only and event_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"Для event-only {item_label} потрібно вибрати івент.",
        )
    if event_id is not None and not db.get(GameEvent, event_id):
        raise HTTPException(status_code=400, detail="Вибраний івент не існує.")
    return event_id if event_only else None


def seed_dice(db: Session) -> None:
    if db.scalar(select(func.count(Dice.id))):
        return
    db.add_all(
        [
            Dice(
                name="Origin Cube",
                description="Чистий білий куб для перших розломів.",
                face_color="#f2f3f5",
                pip_color="#17181b",
                base_luck=1,
                unlock_cost=0,
                upgrade_base_cost=750,
                luck_growth=1.42,
                max_level=12,
                required_level=1,
                sort_order=0,
                is_starter=True,
            ),
            Dice(
                name="Azure Pulse",
                description="Стабільний рідкісний куб для середини прогресії.",
                face_color="#d9e7ff",
                pip_color="#1f4f9b",
                base_luck=25,
                unlock_cost=25_000,
                upgrade_base_cost=18_000,
                luck_growth=1.38,
                max_level=10,
                required_level=4,
                sort_order=10,
            ),
            Dice(
                name="Eclipse Core",
                description="Важкий івентовий куб із тисячами одиниць luck.",
                face_color="#312f3d",
                pip_color="#f0b232",
                base_luck=2_500,
                unlock_cost=2_000_000,
                upgrade_base_cost=1_250_000,
                luck_growth=1.32,
                max_level=9,
                required_level=12,
                sort_order=20,
            ),
            Dice(
                name="Sovereign d6",
                description="Ендґейм-куб із базовими 240 000 luck.",
                face_color="#eeeaff",
                pip_color="#5c43a8",
                base_luck=240_000,
                unlock_cost=350_000_000,
                upgrade_base_cost=225_000_000,
                luck_growth=1.28,
                max_level=8,
                required_level=30,
                sort_order=30,
            ),
        ]
    )
    db.commit()


def seed_database(db: Session) -> None:
    if db.scalar(select(func.count(Card.id))):
        seed_dice(db)
        return

    solar = GameEvent(
        name="Solar Eclipse",
        description="Чорне сонце відкрилося. У пулі з’явилися унікальні Mythic та Paragon картки.",
        accent="#f0b232",
        start_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 18, 23, 59, tzinfo=timezone.utc),
        status="active",
    )
    frozen = GameEvent(
        name="Frozen Moon",
        description="Крижаний розлом принесе Celestial-картки та мутацію Frozen.",
        accent="#65d8ff",
        start_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc),
        status="scheduled",
    )
    lava = GameEvent(
        name="Lava Surge",
        description="Вулканічні картки повернуться, коли адміністратор запустить подію.",
        accent="#f23f42",
        status="paused",
    )
    db.add_all([solar, frozen, lava])
    db.flush()

    shiny = Mutation(name="Shiny", color="#d8f7ff", chance=0.08, income_multiplier=1.35)
    golden = Mutation(name="Golden", color="#ffd45c", chance=0.025, income_multiplier=2.0)
    glitched = Mutation(name="Glitched", color="#b86bff", chance=0.007, income_multiplier=4.0)
    eclipsed = Mutation(name="Eclipsed", color="#ff8a3d", chance=0.012, income_multiplier=5.0, event_only=True, event_id=solar.id)
    lava_mutation = Mutation(name="Lava", color="#ff4938", chance=0.04, income_multiplier=3.0, event_only=True, event_id=lava.id)
    frozen_mutation = Mutation(name="Frozen", color="#6de7ff", chance=0.03, income_multiplier=2.8, event_only=True, event_id=frozen.id)
    db.add_all([shiny, golden, glitched, eclipsed, lava_mutation, frozen_mutation])
    db.flush()

    cards = [
        Card(name="Astra Scout", subtitle="Перше світло Azure Order", rarity_name="Common", rarity_tier=0, rarity_color="#949ba4", base_weight=65000, income_per_second=4, mutations=[shiny]),
        Card(name="Ember Blade", subtitle="Хранитель останньої сонячної іскри", rarity_name="Rare", rarity_tier=1, rarity_color="#50a7ff", base_weight=18000, income_per_second=26, mutations=[shiny, golden]),
        Card(name="Ocean Vow", subtitle="Оракул під скляним припливом", rarity_name="Epic", rarity_tier=2, rarity_color="#a56bff", base_weight=4200, income_per_second=160, mutations=[shiny, golden, glitched]),
        Card(name="Lune Herald", subtitle="Голос із розколотого місяця", rarity_name="Legendary", rarity_tier=3, rarity_color="#ffb13d", base_weight=620, income_per_second=1100, mutations=[shiny, golden, glitched]),
        Card(name="Solara, Eclipse Warden", subtitle="Вона охороняє чорне сонце", rarity_name="Mythic", rarity_tier=4, rarity_color="#ff5d72", base_weight=42, income_per_second=12000, event_only=True, event_id=solar.id, mutations=[shiny, golden, eclipsed]),
        Card(name="Nyx, Rift Sovereign", subtitle="За межами самої ймовірності", rarity_name="Paragon", rarity_tier=5, rarity_color="#e8dcff", base_weight=2.2, income_per_second=145000, event_only=True, event_id=solar.id, mutations=[golden, glitched, eclipsed]),
        Card(name="Cinder Revenant", subtitle="Чекає наступного Lava Surge", rarity_name="Mythic", rarity_tier=4, rarity_color="#ff4938", base_weight=18, income_per_second=32000, event_only=True, event_id=lava.id, mutations=[golden, lava_mutation]),
        Card(name="Kael, Azure Oracle", subtitle="Пророцтво, запечатане в кризі", rarity_name="Celestial", rarity_tier=5, rarity_color="#74e7ff", base_weight=0.7, income_per_second=420000, event_only=True, event_id=frozen.id, mutations=[shiny, glitched, frozen_mutation]),
    ]
    db.add_all(cards)
    db.add_all(
        [
            Potion(name="Lucky Tonic", description="+50% тиску удачі на рідкісні картки протягом 10 хвилин.", effect_type="luck", multiplier=1.5, duration_seconds=600, color="#23a55a", starter_quantity=3),
            Potion(name="Fortune Elixir", description="2.25× удачі для короткої п’ятихвилинної серії.", effect_type="luck", multiplier=2.25, duration_seconds=300, color="#f0b232", starter_quantity=1),
            Potion(name="Swift Draught", description="Подвоює швидкість ролу на 8 хвилин.", effect_type="speed", multiplier=2.0, duration_seconds=480, color="#50a7ff", starter_quantity=2),
        ]
    )
    db.commit()
    seed_dice(db)


def default_potion_price(potion: Potion) -> int:
    minutes = max(1, potion.duration_seconds // 60)
    return max(750, int(potion.multiplier * minutes * 360))


def ensure_shop_offers(db: Session) -> None:
    existing = {
        offer.potion_id
        for offer in db.scalars(select(PotionShopOffer)).all()
    }
    changed = False
    for potion in db.scalars(select(Potion)).all():
        if potion.id in existing:
            continue
        stock = 8 if potion.multiplier >= 2 else 16
        db.add(
            PotionShopOffer(
                potion_id=potion.id,
                price=default_potion_price(potion),
                pack_size=1,
                stock_limit=stock,
                stock_remaining=stock,
                restock_seconds=300,
                restocked_at=utcnow(),
                required_rebirths=0,
                is_active=potion.is_active,
            )
        )
        changed = True
    if changed:
        db.commit()


def seed_rebirths(db: Session) -> None:
    if db.scalar(select(func.count(RebirthTier.id))):
        return
    cards = {card.name: card for card in db.scalars(select(Card)).all()}
    definitions = [
        {
            "tier": 1,
            "name": "Перший розлом",
            "description": "Перший постійний стрибок luck та доходу.",
            "required_coins": 50_000,
            "luck_multiplier": 1.20,
            "income_multiplier": 1.20,
            "accent": "#57f287",
            "requirements": [("Astra Scout", 15), ("Ember Blade", 3)],
        },
        {
            "tier": 2,
            "name": "Стабілізація Rift",
            "description": "Сильніший постійний бонус для середини прогресії.",
            "required_coins": 650_000,
            "luck_multiplier": 1.30,
            "income_multiplier": 1.35,
            "accent": "#50a7ff",
            "requirements": [("Ember Blade", 18), ("Ocean Vow", 5)],
        },
        {
            "tier": 3,
            "name": "Ascendant",
            "description": "Відкриває ендґейм-магазин і різко прискорює фарм.",
            "required_coins": 8_000_000,
            "luck_multiplier": 1.50,
            "income_multiplier": 1.60,
            "accent": "#a56bff",
            "requirements": [("Ocean Vow", 24), ("Lune Herald", 6)],
        },
        {
            "tier": 4,
            "name": "Sovereign",
            "description": "Пізній ребірт для найсильніших колекцій.",
            "required_coins": 120_000_000,
            "luck_multiplier": 1.80,
            "income_multiplier": 2.00,
            "accent": "#f0b232",
            "requirements": [("Lune Herald", 20), ("Solara, Eclipse Warden", 3)],
        },
    ]
    for definition in definitions:
        requirement_defs = definition.pop("requirements")
        tier = RebirthTier(**definition)
        db.add(tier)
        db.flush()
        for card_name, quantity in requirement_defs:
            card = cards.get(card_name)
            if card:
                db.add(
                    RebirthRequirement(
                        rebirth_tier_id=tier.id,
                        card_id=card.id,
                        quantity=quantity,
                    )
                )
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("RENDER") and RAW_DATABASE_URL == "sqlite:///./rift_roll.db":
        raise RuntimeError("DATABASE_URL is required on Render. Paste the Aiven PostgreSQL Service URI.")
    if os.getenv("RENDER") and SECRET_KEY == "change-this-secret-before-production":
        raise RuntimeError("SECRET_KEY is required on Render.")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_database(db)
        ensure_shop_offers(db)
        seed_rebirths(db)
    yield


app = FastAPI(title="Rift Roll", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def make_session(user_id: int) -> str:
    expires = int((utcnow() + timedelta(days=SESSION_DAYS)).timestamp())
    payload = f"{user_id}:{expires}"
    signature = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode().rstrip("=")


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        user_id_text, expires_text, supplied = decoded.split(":", 2)
        payload = f"{user_id_text}:{expires_text}"
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected) or int(expires_text) < int(utcnow().timestamp()):
            return None
        return int(user_id_text)
    except (ValueError, UnicodeDecodeError):
        return None


def set_session_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session(user_id),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def require_user(request: Request, db: DB) -> User:
    user_id = read_session(request.cookies.get(SESSION_COOKIE))
    if user_id is None:
        raise HTTPException(status_code=401, detail="Увійди в акаунт, щоб продовжити.")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Сесію завершено. Увійди ще раз.")
    return user


CurrentUser = Annotated[User, Depends(require_user)]


def user_is_admin(user: User) -> bool:
    return bool(user.is_admin or (ADMIN_EMAIL and user.email.lower() == ADMIN_EMAIL))


def require_admin(user: CurrentUser) -> User:
    if not user_is_admin(user):
        raise HTTPException(status_code=403, detail="Ця дія доступна лише адміністратору.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def event_is_live(event: GameEvent, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if event.status == "active":
        return True
    if event.status != "scheduled":
        return False
    starts = aware(event.start_at) or datetime.min.replace(tzinfo=timezone.utc)
    ends = aware(event.end_at) or datetime.max.replace(tzinfo=timezone.utc)
    return starts <= now <= ends


def live_event_ids(db: Session) -> set[int]:
    return {event.id for event in db.scalars(select(GameEvent)).all() if event_is_live(event)}


def card_available(card: Card, live_ids: set[int]) -> bool:
    return bool(card.is_active and (not card.event_only or (card.event_id in live_ids)))


def mutation_available(mutation: Mutation, live_ids: set[int]) -> bool:
    return bool(mutation.is_active and (not mutation.event_only or (mutation.event_id in live_ids)))


def potion_rows(db: Session, user_id: int) -> list[UserPotion]:
    active_potions = list(
        db.scalars(select(Potion).where(Potion.is_active.is_(True))).all()
    )
    owned_ids = {
        row.potion_id
        for row in db.scalars(
            select(UserPotion).where(UserPotion.user_id == user_id)
        ).all()
    }
    for potion in active_potions:
        if potion.id not in owned_ids:
            db.add(
                UserPotion(
                    user_id=user_id,
                    potion_id=potion.id,
                    quantity=max(0, potion.starter_quantity),
                )
            )
    db.flush()
    return list(
        db.scalars(
            select(UserPotion)
            .join(Potion)
            .where(UserPotion.user_id == user_id, Potion.is_active.is_(True))
            .options(joinedload(UserPotion.potion))
            .order_by(Potion.multiplier)
        ).all()
    )


def effects_for(rows: list[UserPotion]) -> tuple[float, float]:
    now = utcnow()
    luck = 1.0
    speed = 1.0
    for row in rows:
        if not row.active_until or (aware(row.active_until) or now) <= now:
            continue
        if row.potion.effect_type == "luck":
            luck *= row.potion.multiplier
        elif row.potion.effect_type == "speed":
            speed *= row.potion.multiplier
    return min(luck, 1000.0), min(speed, 5.0)


def refresh_offer_stock(offer: PotionShopOffer, now: datetime | None = None) -> None:
    now = now or utcnow()
    restocked_at = aware(offer.restocked_at) or now
    if offer.stock_limit <= 0:
        offer.stock_remaining = -1
        offer.restocked_at = now
        return
    elapsed = max(0, int((now - restocked_at).total_seconds()))
    if elapsed < max(5, offer.restock_seconds):
        return
    cycles = elapsed // max(5, offer.restock_seconds)
    offer.stock_remaining = offer.stock_limit
    offer.restocked_at = restocked_at + timedelta(
        seconds=cycles * max(5, offer.restock_seconds)
    )


def offer_restock_at(offer: PotionShopOffer) -> datetime | None:
    if offer.stock_limit <= 0:
        return None
    return (aware(offer.restocked_at) or utcnow()) + timedelta(
        seconds=max(5, offer.restock_seconds)
    )


def rebirth_progress(db: Session, user_id: int) -> UserRebirth:
    progress = db.get(UserRebirth, user_id)
    if progress:
        return progress
    progress = UserRebirth(user_id=user_id)
    db.add(progress)
    db.flush()
    return progress


def rebirth_multipliers(db: Session, user_id: int) -> tuple[float, float, int]:
    progress = rebirth_progress(db, user_id)
    luck = 1.0
    income = 1.0
    completed = list(
        db.scalars(
            select(RebirthTier)
            .where(
                RebirthTier.is_active.is_(True),
                RebirthTier.tier <= progress.completed_tier,
            )
            .order_by(RebirthTier.tier)
        ).all()
    )
    for tier in completed:
        luck *= tier.luck_multiplier
        income *= tier.income_multiplier
    return min(luck, 1_000_000.0), min(income, 1_000_000.0), progress.completed_tier


def dice_luck(die: Dice, level: int) -> float:
    level = max(1, min(level, die.max_level))
    return min(1_000_000_000.0, die.base_luck * (die.luck_growth ** (level - 1)))


def dice_upgrade_cost(die: Dice, level: int) -> int | None:
    if level >= die.max_level:
        return None
    return max(1, int(die.upgrade_base_cost * (2.05 ** (level - 1))))


def ensure_user_dice(db: Session, user_id: int) -> list[UserDice]:
    rows = list(
        db.scalars(
            select(UserDice)
            .where(UserDice.user_id == user_id)
            .options(joinedload(UserDice.dice))
            .order_by(UserDice.acquired_at)
        ).all()
    )
    starter = db.scalar(
        select(Dice)
        .where(Dice.is_active.is_(True))
        .order_by(Dice.is_starter.desc(), Dice.unlock_cost, Dice.sort_order)
        .limit(1)
    )
    if not rows and starter:
        owned = UserDice(user_id=user_id, dice_id=starter.id, level=1, is_equipped=True)
        db.add(owned)
        db.flush()
        rows = [owned]
        owned.dice = starter
    active_rows = [row for row in rows if row.dice and row.dice.is_active]
    equipped = [row for row in active_rows if row.is_equipped]
    if active_rows and not equipped:
        active_rows[0].is_equipped = True
        equipped = [active_rows[0]]
    if len(equipped) > 1:
        for row in equipped[1:]:
            row.is_equipped = False
    db.flush()
    return rows


def equipped_dice(rows: list[UserDice]) -> UserDice | None:
    active = [row for row in rows if row.dice and row.dice.is_active]
    return next((row for row in active if row.is_equipped), active[0] if active else None)


def serialize_dice(die: Dice, owned: UserDice | None = None) -> dict[str, Any]:
    level = owned.level if owned else 1
    return {
        "id": die.id,
        "name": die.name,
        "description": die.description,
        "faceColor": die.face_color,
        "pipColor": die.pip_color,
        "textureUrl": die.texture_url,
        "baseLuck": die.base_luck,
        "currentLuck": dice_luck(die, level),
        "unlockCost": die.unlock_cost,
        "upgradeBaseCost": die.upgrade_base_cost,
        "nextUpgradeCost": dice_upgrade_cost(die, level),
        "luckGrowth": die.luck_growth,
        "maxLevel": die.max_level,
        "requiredLevel": die.required_level,
        "sortOrder": die.sort_order,
        "isStarter": die.is_starter,
        "isActive": die.is_active,
        "owned": owned is not None,
        "level": level if owned else 0,
        "isEquipped": bool(owned and owned.is_equipped),
    }


def avatar_for(db: Session, user_id: int) -> str:
    row = db.get(UserAvatar, user_id)
    return row.avatar_url if row else ""


def inventory_rows(db: Session, user_id: int) -> list[InventoryItem]:
    return list(
        db.scalars(
            select(InventoryItem)
            .where(InventoryItem.user_id == user_id)
            .options(joinedload(InventoryItem.card), joinedload(InventoryItem.mutation))
            .order_by(InventoryItem.last_obtained_at.desc())
        ).all()
    )


def item_income(item: InventoryItem) -> float:
    mutation_multiplier = item.mutation.income_multiplier if item.mutation else 1.0
    return item.card.income_per_second * mutation_multiplier * item.quantity


def accrue_income(db: Session, user: User, items: list[InventoryItem] | None = None) -> int:
    now = utcnow()
    last = aware(user.last_income_at) or now
    elapsed = max(0, min(86400, int((now - last).total_seconds())))
    if elapsed < 1:
        return 0
    items = items if items is not None else inventory_rows(db, user.id)
    _, income_multiplier, _ = rebirth_multipliers(db, user.id)
    earned = int(
        sum(item_income(item) for item in items) * elapsed * income_multiplier
    )
    user.coins += earned
    user.last_income_at = now
    db.flush()
    return earned


def serialize_event(event: GameEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "name": event.name,
        "description": event.description,
        "accent": event.accent,
        "startAt": iso(event.start_at),
        "endAt": iso(event.end_at),
        "status": event.status,
        "isLive": event_is_live(event),
    }


def serialize_mutation(mutation: Mutation, live_ids: set[int]) -> dict[str, Any]:
    return {
        "id": mutation.id,
        "name": mutation.name,
        "color": mutation.color,
        "chance": mutation.chance,
        "incomeMultiplier": mutation.income_multiplier,
        "eventOnly": mutation.event_only,
        "eventId": mutation.event_id,
        "isActive": mutation.is_active,
        "availableNow": mutation_available(mutation, live_ids),
    }


def serialize_card(card: Card, live_ids: set[int]) -> dict[str, Any]:
    return {
        "id": card.id,
        "name": card.name,
        "subtitle": card.subtitle,
        "imageUrl": card.image_url,
        "rarityName": card.rarity_name,
        "rarityTier": card.rarity_tier,
        "rarityColor": card.rarity_color,
        "baseWeight": card.base_weight,
        "incomePerSecond": card.income_per_second,
        "eventOnly": card.event_only,
        "eventId": card.event_id,
        "isActive": card.is_active,
        "availableNow": card_available(card, live_ids),
        "mutationIds": [mutation.id for mutation in card.mutations],
    }


def serialize_inventory(item: InventoryItem) -> dict[str, Any]:
    mutation = item.mutation
    return {
        "id": item.id,
        "cardId": item.card_id,
        "mutationId": item.mutation_id,
        "mutationKey": item.mutation_key,
        "quantity": item.quantity,
        "firstObtainedAt": iso(item.first_obtained_at),
        "lastObtainedAt": iso(item.last_obtained_at),
        "name": item.card.name,
        "subtitle": item.card.subtitle,
        "imageUrl": item.card.image_url,
        "rarityName": item.card.rarity_name,
        "rarityTier": item.card.rarity_tier,
        "rarityColor": item.card.rarity_color,
        "incomePerSecond": item.card.income_per_second,
        "mutationName": mutation.name if mutation else None,
        "mutationColor": mutation.color if mutation else None,
        "incomeMultiplier": mutation.income_multiplier if mutation else 1.0,
    }


def serialize_shop_offer(
    offer: PotionShopOffer,
    owned: UserPotion | None,
    completed_rebirths: int,
) -> dict[str, Any]:
    refresh_offer_stock(offer)
    potion = offer.potion
    stock = None if offer.stock_limit <= 0 else max(0, offer.stock_remaining)
    return {
        "potionId": potion.id,
        "name": potion.name,
        "description": potion.description,
        "effectType": potion.effect_type,
        "multiplier": potion.multiplier,
        "durationSeconds": potion.duration_seconds,
        "color": potion.color,
        "ownedQuantity": owned.quantity if owned else 0,
        "price": offer.price,
        "packSize": offer.pack_size,
        "stockLimit": offer.stock_limit,
        "stockRemaining": stock,
        "restockSeconds": offer.restock_seconds,
        "restockAt": iso(offer_restock_at(offer)),
        "requiredRebirths": offer.required_rebirths,
        "isActive": bool(offer.is_active and potion.is_active),
        "unlocked": completed_rebirths >= offer.required_rebirths,
    }


def requirement_owned_count(
    db: Session,
    user_id: int,
    requirement: RebirthRequirement,
) -> int:
    query = select(func.coalesce(func.sum(InventoryItem.quantity), 0)).where(
        InventoryItem.user_id == user_id,
        InventoryItem.card_id == requirement.card_id,
    )
    if requirement.mutation_id is not None:
        query = query.where(InventoryItem.mutation_id == requirement.mutation_id)
    return int(db.scalar(query) or 0)


def serialize_rebirth_tier(
    db: Session,
    tier: RebirthTier,
    user_id: int | None = None,
) -> dict[str, Any]:
    requirements = []
    for requirement in tier.requirements:
        owned = (
            requirement_owned_count(db, user_id, requirement)
            if user_id is not None
            else None
        )
        requirements.append(
            {
                "id": requirement.id,
                "cardId": requirement.card_id,
                "cardName": requirement.card.name,
                "cardImageUrl": requirement.card.image_url,
                "rarityName": requirement.card.rarity_name,
                "rarityColor": requirement.card.rarity_color,
                "mutationId": requirement.mutation_id,
                "mutationName": requirement.mutation.name if requirement.mutation else None,
                "mutationColor": requirement.mutation.color if requirement.mutation else None,
                "quantity": requirement.quantity,
                "owned": owned,
                "ready": owned is None or owned >= requirement.quantity,
            }
        )
    return {
        "id": tier.id,
        "tier": tier.tier,
        "name": tier.name,
        "description": tier.description,
        "requiredCoins": tier.required_coins,
        "luckMultiplier": tier.luck_multiplier,
        "incomeMultiplier": tier.income_multiplier,
        "accent": tier.accent,
        "isActive": tier.is_active,
        "requirements": requirements,
    }


def rebirth_payload(db: Session, user: User) -> dict[str, Any]:
    progress = rebirth_progress(db, user.id)
    luck_multiplier, income_multiplier, completed_tier = rebirth_multipliers(
        db, user.id
    )
    tiers = list(
        db.scalars(
            select(RebirthTier)
            .where(RebirthTier.is_active.is_(True))
            .options(
                joinedload(RebirthTier.requirements).joinedload(
                    RebirthRequirement.card
                ),
                joinedload(RebirthTier.requirements).joinedload(
                    RebirthRequirement.mutation
                ),
            )
            .order_by(RebirthTier.tier)
        ).unique().all()
    )
    next_tier = next((tier for tier in tiers if tier.tier > completed_tier), None)
    serialized_next = (
        serialize_rebirth_tier(db, next_tier, user.id) if next_tier else None
    )
    if serialized_next:
        serialized_next["coinsOwned"] = user.coins
        serialized_next["coinsReady"] = user.coins >= next_tier.required_coins
        serialized_next["ready"] = serialized_next["coinsReady"] and all(
            requirement["ready"] for requirement in serialized_next["requirements"]
        )
    return {
        "completedTier": completed_tier,
        "luckMultiplier": luck_multiplier,
        "incomeMultiplier": income_multiplier,
        "totalCoinsSpent": progress.total_coins_spent,
        "lastRebirthAt": iso(progress.last_rebirth_at),
        "next": serialized_next,
        "tiers": [serialize_rebirth_tier(db, tier, user.id) for tier in tiers],
        "maxed": next_tier is None,
    }


def hourly_champion(db: Session) -> dict[str, Any] | None:
    hour_start = utcnow().replace(minute=0, second=0, microsecond=0)
    roll = db.scalar(
        select(RollHistory)
        .join(Card, RollHistory.card_id == Card.id)
        .outerjoin(Mutation, RollHistory.mutation_id == Mutation.id)
        .where(RollHistory.rolled_at >= hour_start)
        .options(
            joinedload(RollHistory.user),
            joinedload(RollHistory.card),
            joinedload(RollHistory.mutation),
        )
        .order_by(
            (RollHistory.adjusted_chance * func.coalesce(Mutation.chance, 1.0)).asc(),
            Card.rarity_tier.desc(),
            RollHistory.rolled_at.desc(),
        )
        .limit(1)
    )
    if not roll:
        return None
    mutation_chance = roll.mutation.chance if roll.mutation else 1.0
    denominator = 1 / max(0.000000000001, roll.adjusted_chance * mutation_chance)
    return {
        "id": roll.id,
        "playerId": roll.user_id,
        "playerHandle": roll.user.handle,
        "playerName": roll.user.display_name,
        "playerAvatarUrl": avatar_for(db, roll.user_id),
        "rolledAt": iso(roll.rolled_at),
        "effectiveLuck": roll.effective_luck,
        "cardId": roll.card_id,
        "name": roll.card.name,
        "subtitle": roll.card.subtitle,
        "imageUrl": roll.card.image_url,
        "rarityName": roll.card.rarity_name,
        "rarityTier": roll.card.rarity_tier,
        "rarityColor": roll.card.rarity_color,
        "mutationId": roll.mutation_id,
        "mutationName": roll.mutation.name if roll.mutation else None,
        "mutationColor": roll.mutation.color if roll.mutation else None,
        "oddsDenominator": denominator,
        "incomePerSecond": roll.card.income_per_second * (roll.mutation.income_multiplier if roll.mutation else 1),
    }


def game_snapshot(db: Session, user: User) -> dict[str, Any]:
    events = list(db.scalars(select(GameEvent).order_by(GameEvent.created_at.desc())).all())
    live_ids = {event.id for event in events if event_is_live(event)}
    cards = list(
        db.scalars(
            select(Card)
            .where(Card.is_active.is_(True))
            .options(joinedload(Card.mutations))
            .order_by(Card.rarity_tier.desc(), Card.base_weight.asc())
        ).unique().all()
    )
    mutations = list(db.scalars(select(Mutation).order_by(Mutation.chance.asc())).all())
    items = inventory_rows(db, user.id)
    accrue_income(db, user, items)
    potions = potion_rows(db, user.id)
    potion_luck, speed = effects_for(potions)
    owned_dice = ensure_user_dice(db, user.id)
    equipped = equipped_dice(owned_dice)
    die_luck = dice_luck(equipped.dice, equipped.level) if equipped else 1.0
    rebirth_luck, rebirth_income, completed_rebirths = rebirth_multipliers(
        db, user.id
    )
    luck = min(1_000_000_000.0, die_luck * potion_luck * rebirth_luck)
    dice_catalog = list(
        db.scalars(
            select(Dice)
            .where(Dice.is_active.is_(True))
            .order_by(Dice.sort_order, Dice.unlock_cost, Dice.id)
        ).all()
    )
    owned_by_dice = {row.dice_id: row for row in owned_dice}
    total_income = sum(item_income(item) for item in items) * rebirth_income
    potion_by_id = {row.potion_id: row for row in potions}
    shop_offers = list(
        db.scalars(
            select(PotionShopOffer)
            .join(Potion)
            .where(
                PotionShopOffer.is_active.is_(True),
                Potion.is_active.is_(True),
            )
            .options(joinedload(PotionShopOffer.potion))
            .order_by(PotionShopOffer.required_rebirths, PotionShopOffer.price)
        ).all()
    )
    for offer in shop_offers:
        refresh_offer_stock(offer)
    history = list(
        db.scalars(
            select(RollHistory)
            .where(RollHistory.user_id == user.id)
            .options(joinedload(RollHistory.card), joinedload(RollHistory.mutation))
            .order_by(RollHistory.rolled_at.desc())
            .limit(8)
        ).all()
    )
    db.commit()
    return {
        "player": {
            "id": user.id,
            "displayName": user.display_name,
            "handle": user.handle,
            "bio": user.bio,
            "accent": user.accent,
            "avatarUrl": avatar_for(db, user.id),
            "joinedAt": iso(user.created_at),
            "isAdmin": user_is_admin(user),
            "level": user.level,
            "xp": user.xp,
            "coins": user.coins,
            "rolls": user.rolls,
            "lastRollAt": iso(user.last_roll_at),
            "luck": luck,
            "diceLuck": die_luck,
            "potionLuck": potion_luck,
            "rebirthLuck": rebirth_luck,
            "rebirthIncome": rebirth_income,
            "rebirths": completed_rebirths,
            "equippedDiceId": equipped.dice_id if equipped else None,
            "speed": speed,
            "totalIncomePerSecond": total_income,
            "uniqueOwned": len({item.card_id for item in items}),
            "totalCards": len(cards),
        },
        "cards": [serialize_card(card, live_ids) for card in cards],
        "mutations": [serialize_mutation(mutation, live_ids) for mutation in mutations],
        "events": [serialize_event(event) for event in events],
        "dice": [serialize_dice(die, owned_by_dice.get(die.id)) for die in dice_catalog],
        "equippedDice": serialize_dice(equipped.dice, equipped) if equipped else None,
        "inventory": [serialize_inventory(item) for item in items],
        "potions": [
            {
                "id": row.potion.id,
                "name": row.potion.name,
                "description": row.potion.description,
                "effectType": row.potion.effect_type,
                "multiplier": row.potion.multiplier,
                "durationSeconds": row.potion.duration_seconds,
                "color": row.potion.color,
                "quantity": row.quantity,
                "activeUntil": iso(row.active_until),
            }
            for row in potions
        ],
        "shop": {
            "potions": [
                serialize_shop_offer(
                    offer,
                    potion_by_id.get(offer.potion_id),
                    completed_rebirths,
                )
                for offer in shop_offers
            ],
        },
        "rebirth": rebirth_payload(db, user),
        "history": [
            {
                "id": entry.id,
                "rolledAt": iso(entry.rolled_at),
                "effectiveLuck": entry.effective_luck,
                "adjustedChance": entry.adjusted_chance,
                "cardId": entry.card_id,
                "name": entry.card.name,
                "imageUrl": entry.card.image_url,
                "rarityName": entry.card.rarity_name,
                "rarityColor": entry.card.rarity_color,
                "mutationId": entry.mutation_id,
                "mutationName": entry.mutation.name if entry.mutation else None,
                "mutationColor": entry.mutation.color if entry.mutation else None,
            }
            for entry in history
        ],
        "hourlyChampion": hourly_champion(db),
        "hourEndsAt": iso(utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)),
    }


def profile_payload(db: Session, viewer: User, target_handle: str | None = None) -> dict[str, Any]:
    target = viewer
    if target_handle:
        target = db.scalar(select(User).where(User.handle == target_handle.lower().removeprefix("@")))
        if not target:
            raise HTTPException(status_code=404, detail="Профіль гравця не знайдено.")
    items = inventory_rows(db, target.id)
    slots = list(
        db.scalars(
            select(ShowcaseSlot)
            .where(ShowcaseSlot.user_id == target.id)
            .options(
                joinedload(ShowcaseSlot.inventory).joinedload(InventoryItem.card),
                joinedload(ShowcaseSlot.inventory).joinedload(InventoryItem.mutation),
            )
            .order_by(ShowcaseSlot.page_index, ShowcaseSlot.slot_index)
        ).all()
    )
    target_dice_rows = list(
        db.scalars(
            select(UserDice)
            .where(UserDice.user_id == target.id)
            .options(joinedload(UserDice.dice))
        ).all()
    )
    target_equipped = equipped_dice(target_dice_rows)
    target_rebirth_luck, target_rebirth_income, target_rebirths = rebirth_multipliers(
        db, target.id
    )
    total_cards = db.scalar(select(func.count(Card.id)).where(Card.is_active.is_(True))) or 0
    return {
        "isOwner": target.id == viewer.id,
        "profile": {
            "id": target.id,
            "displayName": target.display_name,
            "handle": target.handle,
            "bio": target.bio,
            "accent": target.accent,
            "avatarUrl": avatar_for(db, target.id),
            "level": target.level,
            "rolls": target.rolls,
            "coins": target.coins,
            "joinedAt": iso(target.created_at),
            "uniqueOwned": len({item.card_id for item in items}),
            "ownedVariants": len(items),
            "totalCards": total_cards,
            "totalIncomePerSecond": sum(item_income(item) for item in items)
            * target_rebirth_income,
            "rebirths": target_rebirths,
            "rebirthLuck": target_rebirth_luck,
            "rebirthIncome": target_rebirth_income,
            "equippedDice": serialize_dice(target_equipped.dice, target_equipped) if target_equipped else None,
        },
        "showcase": [
            {
                "pageIndex": slot.page_index,
                "slotIndex": slot.slot_index,
                **serialize_inventory(slot.inventory),
            }
            for slot in slots
        ],
        "inventoryOptions": [serialize_inventory(item) for item in items] if target.id == viewer.id else [],
    }


class RegisterInput(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=32)
    handle: str = Field(min_length=3, max_length=20)


class LoginInput(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)


class ProfileInput(BaseModel):
    display_name: str = Field(min_length=2, max_length=32)
    handle: str = Field(min_length=3, max_length=20)
    bio: str = Field(default="", max_length=190)


class ShowcaseInput(BaseModel):
    page_index: int = Field(ge=0, le=3)
    slot_index: int = Field(ge=0, le=7)
    inventory_id: int | None = None


class CardInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    subtitle: str = Field(default="", max_length=160)
    image_url: str = Field(default="", max_length=2000)
    rarity_name: str = Field(default="Common", min_length=1, max_length=32)
    rarity_tier: int = Field(default=0, ge=0, le=10)
    rarity_color: str = Field(default="#949ba4", max_length=16)
    base_weight: float = Field(gt=0)
    income_per_second: int = Field(default=1, ge=0)
    event_only: bool = False
    event_id: int | None = None
    is_active: bool = True
    mutation_ids: list[int] = Field(default_factory=list)


class MutationInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = Field(default="#ffffff", max_length=16)
    chance: float = Field(gt=0, le=100)
    income_multiplier: float = Field(default=1, ge=1, le=1000)
    event_only: bool = False
    event_id: int | None = None
    is_active: bool = True


class EventInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    accent: str = Field(default="#f0b232", max_length=16)
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str = "scheduled"


class PotionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=220)
    effect_type: str
    multiplier: float = Field(gt=1, le=100)
    duration_seconds: int = Field(ge=5, le=604800)
    color: str = Field(default="#5865f2", max_length=16)
    starter_quantity: int = Field(default=0, ge=0, le=1000)
    is_active: bool = True
    shop_price: int = Field(default=1000, ge=0, le=10**18)
    pack_size: int = Field(default=1, ge=1, le=1000)
    stock_limit: int = Field(default=10, ge=0, le=1_000_000)
    restock_seconds: int = Field(default=300, ge=5, le=604800)
    required_rebirths: int = Field(default=0, ge=0, le=1_000_000)
    shop_active: bool = True


class ShopPurchaseInput(BaseModel):
    packs: int = Field(default=1, ge=1, le=100)


class RebirthRequirementInput(BaseModel):
    card_id: int
    mutation_id: int | None = None
    quantity: int = Field(ge=1, le=1_000_000)


class RebirthInput(BaseModel):
    tier: int = Field(ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)
    required_coins: int = Field(ge=0, le=10**18)
    luck_multiplier: float = Field(default=1.15, gt=1, le=1000)
    income_multiplier: float = Field(default=1.15, gt=1, le=1000)
    accent: str = Field(default="#5865f2", max_length=16)
    is_active: bool = True
    requirements: list[RebirthRequirementInput] = Field(
        default_factory=list, max_length=20
    )


class DiceInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=220)
    face_color: str = Field(default="#f2f3f5", max_length=16)
    pip_color: str = Field(default="#16171a", max_length=16)
    texture_url: str = Field(default="", max_length=2000)
    base_luck: float = Field(default=1, ge=1, le=1_000_000_000)
    unlock_cost: int = Field(default=0, ge=0, le=10**18)
    upgrade_base_cost: int = Field(default=500, ge=1, le=10**18)
    luck_growth: float = Field(default=1.35, ge=1.01, le=10)
    max_level: int = Field(default=10, ge=1, le=100)
    required_level: int = Field(default=1, ge=1, le=1_000_000)
    sort_order: int = Field(default=0, ge=-100_000, le=100_000)
    is_starter: bool = False
    is_active: bool = True


LOGIN_FAILURES: dict[str, list[datetime]] = {}


def check_login_rate(ip: str) -> None:
    cutoff = utcnow() - timedelta(minutes=10)
    attempts = [attempt for attempt in LOGIN_FAILURES.get(ip, []) if attempt > cutoff]
    LOGIN_FAILURES[ip] = attempts
    if len(attempts) >= 10:
        raise HTTPException(status_code=429, detail="Забагато спроб входу. Спробуй через 10 хвилин.")


def upload_image_to_cloud(file: UploadFile, purpose: str) -> dict[str, Any]:
    if not CLOUDINARY_URL:
        raise HTTPException(
            status_code=503,
            detail="Завантаження не налаштоване. Додай CLOUDINARY_URL у Render Environment.",
        )
    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise HTTPException(status_code=415, detail="Підтримуються JPG, PNG, WEBP або GIF.")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Файл порожній.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Максимальний розмір зображення — 8 МБ.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=415, detail="Файл пошкоджений або не є зображенням.")
    if width < 64 or height < 64:
        raise HTTPException(status_code=400, detail="Зображення має бути щонайменше 64×64 px.")

    options: dict[str, Any] = {
        "folder": f"rift-roll/{purpose}",
        "public_id": f"{purpose}-{secrets.token_hex(10)}",
        "resource_type": "image",
        "format": "webp",
        "quality": "auto:good",
        "overwrite": False,
    }
    if purpose == "avatars":
        options.update(width=512, height=512, crop="fill", gravity="auto")
    elif purpose == "dice":
        options.update(width=640, height=640, crop="fill", gravity="auto")
    else:
        options.update(width=1600, height=2000, crop="limit")
    try:
        result = cloudinary.uploader.upload(io.BytesIO(data), **options)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Хмарне сховище не прийняло зображення.") from exc
    secure_url = str(result.get("secure_url") or "")
    public_id = str(result.get("public_id") or "")
    if not secure_url.startswith("https://") or not public_id:
        raise HTTPException(status_code=502, detail="Сховище не повернуло коректний URL.")
    return {
        "url": secure_url,
        "publicId": public_id,
        "width": result.get("width"),
        "height": result.get("height"),
        "bytes": result.get("bytes"),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser):
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.display_name,
        "handle": user.handle,
        "isAdmin": user_is_admin(user),
    }


@app.post("/api/auth/register")
def register(payload: RegisterInput, db: DB):
    email = payload.email.strip().lower()
    handle = payload.handle.strip().lower().removeprefix("@")
    display_name = payload.display_name.strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Вкажи коректну email-адресу.")
    if not HANDLE_RE.match(handle):
        raise HTTPException(status_code=400, detail="Handle: 3–20 символів, лише a–z, цифри та _.")
    if display_name != payload.display_name.strip() or len(display_name) < 2:
        raise HTTPException(status_code=400, detail="Ім’я має містити від 2 до 32 символів.")
    first_user = (db.scalar(select(func.count(User.id))) or 0) == 0
    user = User(
        email=email,
        password_hash=password_hash(payload.password),
        display_name=display_name,
        handle=handle,
        is_admin=(email == ADMIN_EMAIL) if ADMIN_EMAIL else first_user,
    )
    db.add(user)
    try:
        db.flush()
        for potion in db.scalars(select(Potion)).all():
            db.add(UserPotion(user_id=user.id, potion_id=potion.id, quantity=potion.starter_quantity))
        ensure_user_dice(db, user.id)
        rebirth_progress(db, user.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email або handle уже зайнятий.")
    response = JSONResponse({"ok": True, "displayName": user.display_name})
    set_session_cookie(response, user.id)
    return response


@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request, db: DB):
    ip = request.client.host if request.client else "unknown"
    check_login_rate(ip)
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not password_matches(payload.password, user.password_hash):
        LOGIN_FAILURES.setdefault(ip, []).append(utcnow())
        raise HTTPException(status_code=401, detail="Невірний email або пароль.")
    LOGIN_FAILURES.pop(ip, None)
    response = JSONResponse({"ok": True, "displayName": user.display_name})
    set_session_cookie(response, user.id)
    return response


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, samesite="lax")
    return response


@app.get("/api/game")
def get_game(user: CurrentUser, db: DB):
    return game_snapshot(db, user)


@app.post("/api/roll")
def roll(user: CurrentUser, db: DB):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    potion_inventory = potion_rows(db, user.id)
    potion_luck, speed = effects_for(potion_inventory)
    owned_dice = ensure_user_dice(db, user.id)
    equipped = equipped_dice(owned_dice)
    if not equipped:
        raise HTTPException(status_code=409, detail="Немає активного кубика для ролу.")
    rebirth_luck, rebirth_income, _ = rebirth_multipliers(db, user.id)
    luck = min(
        1_000_000_000.0,
        dice_luck(equipped.dice, equipped.level) * potion_luck * rebirth_luck,
    )
    minimum_delay_ms = max(300, round(1200 / speed))
    now = utcnow()
    if user.last_roll_at:
        elapsed_ms = (now - (aware(user.last_roll_at) or now)).total_seconds() * 1000
        if elapsed_ms < minimum_delay_ms:
            raise HTTPException(
                status_code=429,
                detail="Куб ще заряджається.",
                headers={"Retry-After-Ms": str(max(1, int(minimum_delay_ms - elapsed_ms)))},
            )
    live_ids = live_event_ids(db)
    cards = list(
        db.scalars(
            select(Card)
            .where(Card.is_active.is_(True))
            .options(joinedload(Card.mutations))
        ).unique().all()
    )
    active_cards = [card for card in cards if card_available(card, live_ids)]
    if not active_cards:
        raise HTTPException(status_code=409, detail="Активний пул карток порожній.")
    log_luck = math.log(max(1.0, luck))
    log_weights = [
        (card, math.log(max(card.base_weight, 0.000001)) + card.rarity_tier * 0.62 * log_luck)
        for card in active_cards
    ]
    max_log_weight = max(weight for _, weight in log_weights)
    weighted = [(card, math.exp(weight - max_log_weight)) for card, weight in log_weights]
    total_weight = sum(weight for _, weight in weighted)
    cursor = RNG.random() * total_weight
    selected, selected_weight = weighted[-1]
    for candidate, weight in weighted:
        cursor -= weight
        if cursor <= 0:
            selected, selected_weight = candidate, weight
            break
    triggered = [
        mutation
        for mutation in selected.mutations
        if mutation_available(mutation, live_ids) and RNG.random() < mutation.chance
    ]
    mutation = min(triggered, key=lambda item: item.chance, default=None)
    mutation_key = str(mutation.id) if mutation else "base"
    item = db.scalar(
        select(InventoryItem)
        .where(
            InventoryItem.user_id == user.id,
            InventoryItem.card_id == selected.id,
            InventoryItem.mutation_key == mutation_key,
        )
        .with_for_update()
    )
    if item:
        item.quantity += 1
        item.last_obtained_at = now
    else:
        db.add(
            InventoryItem(
                user_id=user.id,
                card_id=selected.id,
                mutation_id=mutation.id if mutation else None,
                mutation_key=mutation_key,
                quantity=1,
                first_obtained_at=now,
                last_obtained_at=now,
            )
        )
    xp_earned = 6 + selected.rarity_tier * 4
    coins_earned = max(1, int((12 + selected.rarity_tier * 8) * rebirth_income))
    adjusted_chance = selected_weight / total_weight
    user.rolls += 1
    user.xp += xp_earned
    user.level = 1 + user.xp // 100
    user.coins += coins_earned
    user.last_roll_at = now
    db.add(
        RollHistory(
            user_id=user.id,
            card_id=selected.id,
            mutation_id=mutation.id if mutation else None,
            effective_luck=luck,
            adjusted_chance=adjusted_chance,
            rolled_at=now,
        )
    )
    db.commit()
    live_ids = live_event_ids(db)
    result = serialize_card(selected, live_ids)
    result.update(
        {
            "mutation": serialize_mutation(mutation, live_ids) if mutation else None,
            "adjustedChance": adjusted_chance,
            "effectiveLuck": luck,
            "dice": serialize_dice(equipped.dice, equipped),
            "xpEarned": xp_earned,
            "coinsEarned": coins_earned,
        }
    )
    return {"result": result, "snapshot": game_snapshot(db, user)}


@app.post("/api/dice/{dice_id}/buy")
def buy_dice(dice_id: int, user: CurrentUser, db: DB):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    die = db.get(Dice, dice_id)
    if not die or not die.is_active:
        raise HTTPException(status_code=404, detail="Кубик не знайдено.")
    if db.scalar(select(UserDice).where(UserDice.user_id == user.id, UserDice.dice_id == die.id)):
        raise HTTPException(status_code=409, detail="Цей кубик уже відкрито.")
    if user.level < die.required_level:
        raise HTTPException(status_code=409, detail=f"Потрібен рівень {die.required_level}.")
    accrue_income(db, user)
    if user.coins < die.unlock_cost:
        raise HTTPException(status_code=409, detail="Недостатньо Rift Credits.")
    user.coins -= die.unlock_cost
    already_equipped = db.scalar(
        select(func.count(UserDice.id)).where(UserDice.user_id == user.id, UserDice.is_equipped.is_(True))
    )
    db.add(UserDice(user_id=user.id, dice_id=die.id, level=1, is_equipped=not bool(already_equipped)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Цей кубик уже відкрито.")
    return {"snapshot": game_snapshot(db, user)}


@app.post("/api/dice/{dice_id}/equip")
def equip_user_dice(dice_id: int, user: CurrentUser, db: DB):
    owned = db.scalar(
        select(UserDice)
        .where(UserDice.user_id == user.id, UserDice.dice_id == dice_id)
        .options(joinedload(UserDice.dice))
    )
    if not owned or not owned.dice.is_active:
        raise HTTPException(status_code=404, detail="Спочатку відкрий цей кубик.")
    for row in db.scalars(select(UserDice).where(UserDice.user_id == user.id)).all():
        row.is_equipped = row.id == owned.id
    db.commit()
    return {"snapshot": game_snapshot(db, user)}


@app.post("/api/dice/{dice_id}/upgrade")
def upgrade_user_dice(dice_id: int, user: CurrentUser, db: DB):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    owned = db.scalar(
        select(UserDice)
        .where(UserDice.user_id == user.id, UserDice.dice_id == dice_id)
        .options(joinedload(UserDice.dice))
        .with_for_update()
    )
    if not owned or not owned.dice.is_active:
        raise HTTPException(status_code=404, detail="Кубик не знайдено у твоїй колекції.")
    cost = dice_upgrade_cost(owned.dice, owned.level)
    if cost is None:
        raise HTTPException(status_code=409, detail="Кубик уже має максимальний рівень.")
    accrue_income(db, user)
    if user.coins < cost:
        raise HTTPException(status_code=409, detail="Недостатньо Rift Credits для прокачки.")
    user.coins -= cost
    owned.level += 1
    db.commit()
    return {"snapshot": game_snapshot(db, user)}


@app.post("/api/potions/{potion_id}/use")
def use_potion(potion_id: int, user: CurrentUser, db: DB):
    row = db.scalar(
        select(UserPotion)
        .where(UserPotion.user_id == user.id, UserPotion.potion_id == potion_id)
        .options(joinedload(UserPotion.potion))
        .with_for_update()
    )
    if not row or not row.potion.is_active:
        raise HTTPException(status_code=404, detail="Зілля не знайдено.")
    if row.quantity <= 0:
        raise HTTPException(status_code=409, detail="У тебе немає цього зілля.")
    now = utcnow()
    starts_at = max(now, aware(row.active_until) or now)
    row.active_until = starts_at + timedelta(seconds=row.potion.duration_seconds)
    row.quantity -= 1
    db.commit()
    return {"activeUntil": iso(row.active_until), "snapshot": game_snapshot(db, user)}


@app.post("/api/shop/potions/{potion_id}/buy")
def buy_potion(
    potion_id: int,
    payload: ShopPurchaseInput,
    user: CurrentUser,
    db: DB,
):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    offer = db.scalar(
        select(PotionShopOffer)
        .where(PotionShopOffer.potion_id == potion_id)
        .options(joinedload(PotionShopOffer.potion))
        .with_for_update()
    )
    if not offer or not offer.is_active or not offer.potion.is_active:
        raise HTTPException(status_code=404, detail="Цього зілля зараз немає в магазині.")
    refresh_offer_stock(offer)
    progress = rebirth_progress(db, user.id)
    if progress.completed_tier < offer.required_rebirths:
        raise HTTPException(
            status_code=409,
            detail=f"Потрібно {offer.required_rebirths} ребіртів.",
        )
    if offer.stock_limit > 0 and offer.stock_remaining < payload.packs:
        raise HTTPException(status_code=409, detail="Недостатньо товару до наступного restock.")
    accrue_income(db, user)
    total_price = offer.price * payload.packs
    if user.coins < total_price:
        raise HTTPException(status_code=409, detail="Недостатньо Rift Credits.")
    owned = db.scalar(
        select(UserPotion)
        .where(UserPotion.user_id == user.id, UserPotion.potion_id == potion_id)
        .with_for_update()
    )
    if not owned:
        owned = UserPotion(user_id=user.id, potion_id=potion_id, quantity=0)
        db.add(owned)
    user.coins -= total_price
    owned.quantity += offer.pack_size * payload.packs
    if offer.stock_limit > 0:
        offer.stock_remaining -= payload.packs
    db.commit()
    return {
        "purchased": {
            "name": offer.potion.name,
            "quantity": offer.pack_size * payload.packs,
            "spent": total_price,
        },
        "snapshot": game_snapshot(db, user),
    }


@app.post("/api/shop/buy-all-potions")
def buy_all_potions(user: CurrentUser, db: DB):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    accrue_income(db, user)
    progress = rebirth_progress(db, user.id)
    offers = list(
        db.scalars(
            select(PotionShopOffer)
            .join(Potion)
            .where(
                PotionShopOffer.is_active.is_(True),
                Potion.is_active.is_(True),
            )
            .options(joinedload(PotionShopOffer.potion))
            .order_by(PotionShopOffer.price, PotionShopOffer.potion_id)
            .with_for_update()
        ).all()
    )
    purchased: list[dict[str, Any]] = []
    for offer in offers:
        refresh_offer_stock(offer)
        if progress.completed_tier < offer.required_rebirths:
            continue
        if offer.stock_limit > 0 and offer.stock_remaining < 1:
            continue
        if user.coins < offer.price:
            continue
        owned = db.scalar(
            select(UserPotion)
            .where(
                UserPotion.user_id == user.id,
                UserPotion.potion_id == offer.potion_id,
            )
            .with_for_update()
        )
        if not owned:
            owned = UserPotion(
                user_id=user.id,
                potion_id=offer.potion_id,
                quantity=0,
            )
            db.add(owned)
        user.coins -= offer.price
        owned.quantity += offer.pack_size
        if offer.stock_limit > 0:
            offer.stock_remaining -= 1
        purchased.append(
            {
                "name": offer.potion.name,
                "quantity": offer.pack_size,
                "spent": offer.price,
            }
        )
    if not purchased:
        raise HTTPException(
            status_code=409,
            detail="Немає доступних зіль, які можна купити за поточний баланс.",
        )
    db.commit()
    return {"purchased": purchased, "snapshot": game_snapshot(db, user)}


@app.post("/api/rebirth")
def perform_rebirth(user: CurrentUser, db: DB):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update()) or user
    accrue_income(db, user)
    progress = db.scalar(
        select(UserRebirth)
        .where(UserRebirth.user_id == user.id)
        .with_for_update()
    )
    if not progress:
        progress = UserRebirth(user_id=user.id)
        db.add(progress)
        db.flush()
    tier = db.scalar(
        select(RebirthTier)
        .where(
            RebirthTier.is_active.is_(True),
            RebirthTier.tier > progress.completed_tier,
        )
        .options(
            joinedload(RebirthTier.requirements).joinedload(
                RebirthRequirement.card
            ),
            joinedload(RebirthTier.requirements).joinedload(
                RebirthRequirement.mutation
            ),
        )
        .order_by(RebirthTier.tier)
        .limit(1)
    )
    if not tier:
        raise HTTPException(status_code=409, detail="Усі доступні ребірти вже виконано.")
    if user.coins < tier.required_coins:
        raise HTTPException(
            status_code=409,
            detail=f"Потрібно ще {tier.required_coins - user.coins:,} Rift Credits.",
        )
    requirements_and_rows: list[
        tuple[RebirthRequirement, list[InventoryItem]]
    ] = []
    for requirement in tier.requirements:
        query = (
            select(InventoryItem)
            .where(
                InventoryItem.user_id == user.id,
                InventoryItem.card_id == requirement.card_id,
            )
            .options(joinedload(InventoryItem.mutation))
            .with_for_update()
        )
        if requirement.mutation_id is not None:
            query = query.where(
                InventoryItem.mutation_id == requirement.mutation_id
            )
        rows = list(db.scalars(query).all())
        owned = sum(item.quantity for item in rows)
        if owned < requirement.quantity:
            label = requirement.card.name
            if requirement.mutation:
                label += f" · {requirement.mutation.name}"
            raise HTTPException(
                status_code=409,
                detail=f"Не вистачає «{label}»: {owned}/{requirement.quantity}.",
            )
        requirements_and_rows.append((requirement, rows))
    consumed: list[dict[str, Any]] = []
    for requirement, rows in requirements_and_rows:
        left = requirement.quantity
        rows.sort(key=lambda item: (item.mutation_id is not None, item.id))
        for item in rows:
            if left <= 0:
                break
            amount = min(left, item.quantity)
            item.quantity -= amount
            left -= amount
            if item.quantity <= 0:
                for slot in db.scalars(
                    select(ShowcaseSlot).where(
                        ShowcaseSlot.inventory_id == item.id
                    )
                ).all():
                    db.delete(slot)
                db.delete(item)
        consumed.append(
            {
                "cardId": requirement.card_id,
                "name": requirement.card.name,
                "mutationName": requirement.mutation.name
                if requirement.mutation
                else None,
                "quantity": requirement.quantity,
            }
        )
    old_balance = user.coins
    user.coins = 0
    user.last_income_at = utcnow()
    progress.completed_tier = tier.tier
    progress.total_coins_spent += tier.required_coins
    progress.last_rebirth_at = utcnow()
    db.commit()
    return {
        "completed": {
            "tier": tier.tier,
            "name": tier.name,
            "oldBalance": old_balance,
            "consumed": consumed,
            "luckMultiplier": tier.luck_multiplier,
            "incomeMultiplier": tier.income_multiplier,
        },
        "snapshot": game_snapshot(db, user),
    }


@app.get("/api/profile")
def get_profile(user: CurrentUser, db: DB, handle: str | None = None):
    return profile_payload(db, user, handle)


@app.patch("/api/profile")
def update_profile(payload: ProfileInput, user: CurrentUser, db: DB):
    handle = payload.handle.strip().lower().removeprefix("@")
    name = payload.display_name.strip()
    if not HANDLE_RE.match(handle):
        raise HTTPException(status_code=400, detail="Handle: 3–20 символів, лише a–z, цифри та _.")
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Ім’я має містити від 2 до 32 символів.")
    user.display_name = name
    user.handle = handle
    user.bio = payload.bio.strip()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Цей handle уже зайнятий.")
    return profile_payload(db, user)


@app.post("/api/profile/avatar")
def upload_profile_avatar(user: CurrentUser, db: DB, file: UploadFile = File(...)):
    uploaded = upload_image_to_cloud(file, "avatars")
    row = db.get(UserAvatar, user.id)
    previous_public_id = row.public_id if row else ""
    if row:
        row.avatar_url = uploaded["url"]
        row.public_id = uploaded["publicId"]
        row.updated_at = utcnow()
    else:
        db.add(
            UserAvatar(
                user_id=user.id,
                avatar_url=uploaded["url"],
                public_id=uploaded["publicId"],
                updated_at=utcnow(),
            )
        )
    db.commit()
    if previous_public_id and previous_public_id != uploaded["publicId"]:
        try:
            cloudinary.uploader.destroy(previous_public_id, invalidate=True, resource_type="image")
        except Exception:
            pass
    return profile_payload(db, user)


@app.put("/api/profile/showcase")
def update_showcase(payload: ShowcaseInput, user: CurrentUser, db: DB):
    existing = db.scalar(
        select(ShowcaseSlot).where(
            ShowcaseSlot.user_id == user.id,
            ShowcaseSlot.page_index == payload.page_index,
            ShowcaseSlot.slot_index == payload.slot_index,
        )
    )
    if payload.inventory_id is None:
        if existing:
            db.delete(existing)
            db.commit()
        return profile_payload(db, user)
    item = db.scalar(
        select(InventoryItem).where(
            InventoryItem.id == payload.inventory_id,
            InventoryItem.user_id == user.id,
        )
    )
    if not item:
        raise HTTPException(status_code=404, detail="У твоєму інвентарі немає цієї картки.")
    duplicate = db.scalar(
        select(ShowcaseSlot).where(
            ShowcaseSlot.user_id == user.id,
            ShowcaseSlot.inventory_id == item.id,
        )
    )
    if duplicate and duplicate.id != (existing.id if existing else None):
        db.delete(duplicate)
    if existing:
        existing.inventory_id = item.id
        existing.placed_at = utcnow()
    else:
        db.add(
            ShowcaseSlot(
                user_id=user.id,
                inventory_id=item.id,
                page_index=payload.page_index,
                slot_index=payload.slot_index,
            )
        )
    db.commit()
    return profile_payload(db, user)


@app.get("/api/players")
def players(user: CurrentUser, db: DB, q: str = ""):
    pattern = f"%{q.strip().lower()[:32]}%"
    rows = list(
        db.scalars(
            select(User)
            .where(func.lower(User.display_name).like(pattern) | func.lower(User.handle).like(pattern))
            .order_by(User.rolls.desc(), User.display_name.asc())
            .limit(20)
        ).all()
    )
    result = []
    for row in rows:
        variants = db.scalar(select(func.count(InventoryItem.id)).where(InventoryItem.user_id == row.id)) or 0
        result.append(
            {
                "id": row.id,
                "displayName": row.display_name,
                "handle": row.handle,
                "bio": row.bio,
                "accent": row.accent,
                "avatarUrl": avatar_for(db, row.id),
                "level": row.level,
                "rolls": row.rolls,
                "ownedVariants": variants,
                "isViewer": row.id == user.id,
            }
        )
    return {"players": result}


def admin_payload(db: Session) -> dict[str, Any]:
    events = list(db.scalars(select(GameEvent).order_by(GameEvent.created_at.desc())).all())
    live_ids = {event.id for event in events if event_is_live(event)}
    cards = list(db.scalars(select(Card).options(joinedload(Card.mutations)).order_by(Card.rarity_tier.desc())).unique().all())
    mutations = list(db.scalars(select(Mutation).order_by(Mutation.chance.asc())).all())
    potions = list(db.scalars(select(Potion).order_by(Potion.name)).all())
    offers = list(
        db.scalars(
            select(PotionShopOffer).options(joinedload(PotionShopOffer.potion))
        ).all()
    )
    offers_by_potion = {offer.potion_id: offer for offer in offers}
    for offer in offers:
        refresh_offer_stock(offer)
    dice_catalog = list(db.scalars(select(Dice).order_by(Dice.sort_order, Dice.unlock_cost, Dice.id)).all())
    rebirth_tiers = list(
        db.scalars(
            select(RebirthTier)
            .options(
                joinedload(RebirthTier.requirements).joinedload(
                    RebirthRequirement.card
                ),
                joinedload(RebirthTier.requirements).joinedload(
                    RebirthRequirement.mutation
                ),
            )
            .order_by(RebirthTier.tier)
        ).unique().all()
    )
    return {
        "cards": [serialize_card(card, live_ids) for card in cards],
        "dice": [serialize_dice(die) for die in dice_catalog],
        "mutations": [serialize_mutation(mutation, live_ids) for mutation in mutations],
        "events": [serialize_event(event) for event in events],
        "potions": [
            {
                "id": potion.id,
                "name": potion.name,
                "description": potion.description,
                "effectType": potion.effect_type,
                "multiplier": potion.multiplier,
                "durationSeconds": potion.duration_seconds,
                "color": potion.color,
                "starterQuantity": potion.starter_quantity,
                "isActive": potion.is_active,
                "shopPrice": offers_by_potion[potion.id].price
                if potion.id in offers_by_potion
                else default_potion_price(potion),
                "packSize": offers_by_potion[potion.id].pack_size
                if potion.id in offers_by_potion
                else 1,
                "stockLimit": offers_by_potion[potion.id].stock_limit
                if potion.id in offers_by_potion
                else 10,
                "stockRemaining": offers_by_potion[potion.id].stock_remaining
                if potion.id in offers_by_potion
                else 10,
                "restockSeconds": offers_by_potion[potion.id].restock_seconds
                if potion.id in offers_by_potion
                else 300,
                "requiredRebirths": offers_by_potion[potion.id].required_rebirths
                if potion.id in offers_by_potion
                else 0,
                "shopActive": offers_by_potion[potion.id].is_active
                if potion.id in offers_by_potion
                else False,
            }
            for potion in potions
        ],
        "rebirths": [
            serialize_rebirth_tier(db, tier) for tier in rebirth_tiers
        ],
    }


def finalized_admin_payload(db: Session) -> dict[str, Any]:
    payload = admin_payload(db)
    # Stock refreshes happen while the payload is built. Committing here keeps
    # the shop and Admin Studio in sync across workers and page reloads.
    db.commit()
    return payload


@app.get("/api/admin")
def get_admin(_: AdminUser, db: DB):
    return finalized_admin_payload(db)


@app.post("/api/admin/upload")
def admin_upload_image(
    _: AdminUser,
    purpose: str = Form(...),
    file: UploadFile = File(...),
):
    if purpose not in {"cards", "dice"}:
        raise HTTPException(status_code=400, detail="Тип зображення має бути cards або dice.")
    return upload_image_to_cloud(file, purpose)


def apply_card(db: Session, card: Card, payload: CardInput) -> None:
    name = required_admin_text(payload.name, "Назва картки")
    rarity_name = required_admin_text(payload.rarity_name, "Назва рідкості")
    event_id = validate_event_link(
        db,
        event_only=payload.event_only,
        event_id=payload.event_id,
        item_label="картки",
    )
    mutation_ids = list(dict.fromkeys(payload.mutation_ids))
    mutations = (
        list(db.scalars(select(Mutation).where(Mutation.id.in_(mutation_ids))).all())
        if mutation_ids
        else []
    )
    if len(mutations) != len(mutation_ids):
        raise HTTPException(status_code=400, detail="Одна з вибраних мутацій не існує.")
    card.name = name
    card.subtitle = payload.subtitle.strip()
    card.image_url = payload.image_url.strip()
    card.rarity_name = rarity_name
    card.rarity_tier = payload.rarity_tier
    card.rarity_color = clean_color(payload.rarity_color, "#949ba4")
    card.base_weight = payload.base_weight
    card.income_per_second = payload.income_per_second
    card.event_only = payload.event_only
    card.event_id = event_id
    card.is_active = payload.is_active
    card.mutations = mutations


@app.post("/api/admin/cards")
def create_card(payload: CardInput, _: AdminUser, db: DB):
    card = Card(base_weight=payload.base_weight)
    apply_card(db, card, payload)
    db.add(card)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Картку не вдалося створити. Перевір івент і вибрані мутації.",
        )
    return finalized_admin_payload(db)


@app.put("/api/admin/cards/{item_id}")
def update_card(item_id: int, payload: CardInput, _: AdminUser, db: DB):
    card = db.get(Card, item_id)
    if not card:
        raise HTTPException(status_code=404, detail="Картку не знайдено.")
    apply_card(db, card, payload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Картку не вдалося зберегти. Перевір івент і мутації.",
        )
    return finalized_admin_payload(db)


@app.delete("/api/admin/cards/{item_id}")
def delete_card(item_id: int, _: AdminUser, db: DB):
    card = db.get(Card, item_id)
    if not card:
        raise HTTPException(status_code=404, detail="Картку не знайдено.")
    inventory_count = db.scalar(
        select(func.count(InventoryItem.id)).where(InventoryItem.card_id == item_id)
    ) or 0
    history_count = db.scalar(
        select(func.count(RollHistory.id)).where(RollHistory.card_id == item_id)
    ) or 0
    requirement_count = db.scalar(
        select(func.count(RebirthRequirement.id)).where(
            RebirthRequirement.card_id == item_id
        )
    ) or 0
    if inventory_count or history_count or requirement_count:
        raise HTTPException(
            status_code=409,
            detail="Картка вже використовується. Вимкни її замість видалення.",
        )
    db.delete(card)
    db.commit()
    return finalized_admin_payload(db)


def apply_mutation(db: Session, mutation: Mutation, payload: MutationInput) -> None:
    mutation.name = required_admin_text(payload.name, "Назва мутації")
    mutation.color = clean_color(payload.color, "#ffffff")
    mutation.chance = normalize_chance(payload.chance)
    mutation.income_multiplier = payload.income_multiplier
    mutation.event_only = payload.event_only
    mutation.event_id = validate_event_link(
        db,
        event_only=payload.event_only,
        event_id=payload.event_id,
        item_label="мутації",
    )
    mutation.is_active = payload.is_active


@app.post("/api/admin/mutations")
def create_mutation(payload: MutationInput, _: AdminUser, db: DB):
    mutation = Mutation(name=payload.name, chance=normalize_chance(payload.chance))
    apply_mutation(db, mutation, payload)
    db.add(mutation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Мутація з такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.put("/api/admin/mutations/{item_id}")
def update_mutation(item_id: int, payload: MutationInput, _: AdminUser, db: DB):
    mutation = db.get(Mutation, item_id)
    if not mutation:
        raise HTTPException(status_code=404, detail="Мутацію не знайдено.")
    apply_mutation(db, mutation, payload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Мутація з такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.delete("/api/admin/mutations/{item_id}")
def delete_mutation(item_id: int, _: AdminUser, db: DB):
    mutation = db.get(Mutation, item_id)
    if not mutation:
        raise HTTPException(status_code=404, detail="Мутацію не знайдено.")
    requirement_count = db.scalar(
        select(func.count(RebirthRequirement.id)).where(
            RebirthRequirement.mutation_id == item_id
        )
    ) or 0
    if requirement_count:
        raise HTTPException(
            status_code=409,
            detail="Мутація використовується у вимогах ребірту. Спочатку зміни ці вимоги.",
        )
    db.delete(mutation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Мутація вже використовується. Вимкни її замість видалення.",
        )
    return finalized_admin_payload(db)


def apply_event(event: GameEvent, payload: EventInput) -> None:
    if payload.status not in {"scheduled", "active", "paused"}:
        raise HTTPException(status_code=400, detail="Статус має бути scheduled, active або paused.")
    if payload.start_at and payload.end_at and payload.end_at <= payload.start_at:
        raise HTTPException(status_code=400, detail="Кінець івенту має бути після початку.")
    event.name = required_admin_text(payload.name, "Назва івенту")
    event.description = payload.description.strip()
    event.accent = clean_color(payload.accent, "#f0b232")
    event.start_at = payload.start_at
    event.end_at = payload.end_at
    event.status = payload.status


@app.post("/api/admin/events")
def create_event(payload: EventInput, _: AdminUser, db: DB):
    event = GameEvent(name=payload.name)
    apply_event(event, payload)
    db.add(event)
    db.commit()
    return finalized_admin_payload(db)


@app.put("/api/admin/events/{item_id}")
def update_event(item_id: int, payload: EventInput, _: AdminUser, db: DB):
    event = db.get(GameEvent, item_id)
    if not event:
        raise HTTPException(status_code=404, detail="Івент не знайдено.")
    apply_event(event, payload)
    db.commit()
    return finalized_admin_payload(db)


@app.post("/api/admin/events/{item_id}/toggle")
def toggle_event(item_id: int, _: AdminUser, db: DB):
    event = db.get(GameEvent, item_id)
    if not event:
        raise HTTPException(status_code=404, detail="Івент не знайдено.")
    event.status = "paused" if event_is_live(event) else "active"
    db.commit()
    return finalized_admin_payload(db)


@app.delete("/api/admin/events/{item_id}")
def delete_event(item_id: int, _: AdminUser, db: DB):
    event = db.get(GameEvent, item_id)
    if not event:
        raise HTTPException(status_code=404, detail="Івент не знайдено.")
    for card in db.scalars(select(Card).where(Card.event_id == item_id)).all():
        card.event_id = None
        card.event_only = False
    for mutation in db.scalars(select(Mutation).where(Mutation.event_id == item_id)).all():
        mutation.event_id = None
        mutation.event_only = False
    db.delete(event)
    db.commit()
    return finalized_admin_payload(db)


def apply_potion(potion: Potion, payload: PotionInput) -> None:
    if payload.effect_type not in {"luck", "speed"}:
        raise HTTPException(status_code=400, detail="Тип зілля має бути luck або speed.")
    potion.name = required_admin_text(payload.name, "Назва зілля")
    potion.description = payload.description.strip()
    potion.effect_type = payload.effect_type
    potion.multiplier = payload.multiplier
    potion.duration_seconds = payload.duration_seconds
    potion.color = clean_color(payload.color, "#5865f2")
    potion.starter_quantity = payload.starter_quantity
    potion.is_active = payload.is_active


def apply_potion_offer(
    offer: PotionShopOffer,
    payload: PotionInput,
    reset_stock: bool = False,
) -> None:
    previous_limit = offer.stock_limit
    offer.price = payload.shop_price
    offer.pack_size = payload.pack_size
    offer.stock_limit = payload.stock_limit
    offer.restock_seconds = payload.restock_seconds
    offer.required_rebirths = payload.required_rebirths
    offer.is_active = payload.shop_active
    if payload.stock_limit <= 0:
        offer.stock_remaining = -1
    elif reset_stock or previous_limit != payload.stock_limit:
        offer.stock_remaining = payload.stock_limit
        offer.restocked_at = utcnow()
    else:
        offer.stock_remaining = min(
            max(0, offer.stock_remaining), payload.stock_limit
        )


@app.post("/api/admin/potions")
def create_potion(payload: PotionInput, _: AdminUser, db: DB):
    potion = Potion(name=payload.name, effect_type=payload.effect_type, multiplier=payload.multiplier, duration_seconds=payload.duration_seconds)
    apply_potion(potion, payload)
    db.add(potion)
    try:
        db.flush()
        offer = PotionShopOffer(potion_id=potion.id)
        apply_potion_offer(offer, payload, reset_stock=True)
        db.add(offer)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Зілля з такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.put("/api/admin/potions/{item_id}")
def update_potion(item_id: int, payload: PotionInput, _: AdminUser, db: DB):
    potion = db.get(Potion, item_id)
    if not potion:
        raise HTTPException(status_code=404, detail="Зілля не знайдено.")
    apply_potion(potion, payload)
    offer = db.get(PotionShopOffer, potion.id)
    if not offer:
        offer = PotionShopOffer(potion_id=potion.id)
        db.add(offer)
    apply_potion_offer(offer, payload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Зілля з такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.delete("/api/admin/potions/{item_id}")
def delete_potion(item_id: int, _: AdminUser, db: DB):
    potion = db.get(Potion, item_id)
    if not potion:
        raise HTTPException(status_code=404, detail="Зілля не знайдено.")
    offer = db.get(PotionShopOffer, potion.id)
    if offer:
        db.delete(offer)
    db.delete(potion)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Це зілля вже є в інвентарях. Вимкни його замість видалення.",
        )
    return finalized_admin_payload(db)


def apply_rebirth_tier(
    db: Session,
    tier: RebirthTier,
    payload: RebirthInput,
) -> None:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Назва ребірту не може бути порожньою.")
    if not payload.requirements:
        raise HTTPException(
            status_code=400,
            detail="Додай хоча б одну вимогу по картці.",
        )
    duplicate_check: set[tuple[int, int | None]] = set()
    requirements: list[RebirthRequirement] = []
    for item in payload.requirements:
        key = (item.card_id, item.mutation_id)
        if key in duplicate_check:
            raise HTTPException(
                status_code=400,
                detail="Одна й та сама вимога по картці додана двічі.",
            )
        duplicate_check.add(key)
        card = db.get(Card, item.card_id)
        if not card:
            raise HTTPException(status_code=400, detail="Одна з карток не існує.")
        mutation = db.get(Mutation, item.mutation_id) if item.mutation_id else None
        if item.mutation_id and not mutation:
            raise HTTPException(status_code=400, detail="Одна з мутацій не існує.")
        requirements.append(
            RebirthRequirement(
                card_id=card.id,
                mutation_id=mutation.id if mutation else None,
                quantity=item.quantity,
            )
        )
    tier.tier = payload.tier
    tier.name = name
    tier.description = payload.description.strip()
    tier.required_coins = payload.required_coins
    tier.luck_multiplier = payload.luck_multiplier
    tier.income_multiplier = payload.income_multiplier
    tier.accent = clean_color(payload.accent, "#5865f2")
    tier.is_active = payload.is_active
    tier.requirements.clear()
    tier.requirements.extend(requirements)


@app.post("/api/admin/rebirths")
def create_rebirth_tier(payload: RebirthInput, _: AdminUser, db: DB):
    tier = RebirthTier(
        tier=payload.tier,
        name=payload.name.strip(),
        required_coins=payload.required_coins,
    )
    apply_rebirth_tier(db, tier, payload)
    db.add(tier)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ребірт із таким номером уже існує.")
    return finalized_admin_payload(db)


@app.put("/api/admin/rebirths/{item_id}")
def update_rebirth_tier(
    item_id: int,
    payload: RebirthInput,
    _: AdminUser,
    db: DB,
):
    tier = db.get(RebirthTier, item_id)
    if not tier:
        raise HTTPException(status_code=404, detail="Ребірт не знайдено.")
    apply_rebirth_tier(db, tier, payload)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ребірт із таким номером уже існує.")
    return finalized_admin_payload(db)


@app.delete("/api/admin/rebirths/{item_id}")
def delete_rebirth_tier(item_id: int, _: AdminUser, db: DB):
    tier = db.get(RebirthTier, item_id)
    if not tier:
        raise HTTPException(status_code=404, detail="Ребірт не знайдено.")
    completed = db.scalar(
        select(func.count(UserRebirth.user_id)).where(
            UserRebirth.completed_tier >= tier.tier
        )
    ) or 0
    if completed:
        raise HTTPException(
            status_code=409,
            detail="Цей ребірт уже виконували гравці. Вимкни його замість видалення.",
        )
    db.delete(tier)
    db.commit()
    return finalized_admin_payload(db)


def apply_dice(db: Session, die: Dice, payload: DiceInput) -> None:
    die.name = required_admin_text(payload.name, "Назва кубика")
    die.description = payload.description.strip()
    die.face_color = clean_color(payload.face_color, "#f2f3f5")
    die.pip_color = clean_color(payload.pip_color, "#16171a")
    die.texture_url = payload.texture_url.strip()
    die.base_luck = payload.base_luck
    die.unlock_cost = payload.unlock_cost
    die.upgrade_base_cost = payload.upgrade_base_cost
    die.luck_growth = payload.luck_growth
    die.max_level = payload.max_level
    die.required_level = payload.required_level
    die.sort_order = payload.sort_order
    die.is_starter = payload.is_starter
    die.is_active = payload.is_active
    if payload.is_starter:
        for other in db.scalars(select(Dice).where(Dice.id != (die.id or -1), Dice.is_starter.is_(True))).all():
            other.is_starter = False


@app.post("/api/admin/dice")
def create_dice(payload: DiceInput, _: AdminUser, db: DB):
    die = Dice(name=payload.name, base_luck=payload.base_luck)
    apply_dice(db, die, payload)
    db.add(die)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Кубик із такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.put("/api/admin/dice/{item_id}")
def update_dice(item_id: int, payload: DiceInput, _: AdminUser, db: DB):
    die = db.get(Dice, item_id)
    if not die:
        raise HTTPException(status_code=404, detail="Кубик не знайдено.")
    apply_dice(db, die, payload)
    for owned in db.scalars(select(UserDice).where(UserDice.dice_id == die.id, UserDice.level > die.max_level)).all():
        owned.level = die.max_level
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Кубик із такою назвою вже існує.")
    return finalized_admin_payload(db)


@app.delete("/api/admin/dice/{item_id}")
def delete_dice(item_id: int, _: AdminUser, db: DB):
    die = db.get(Dice, item_id)
    if not die:
        raise HTTPException(status_code=404, detail="Кубик не знайдено.")
    owners = db.scalar(select(func.count(UserDice.id)).where(UserDice.dice_id == die.id)) or 0
    if owners:
        raise HTTPException(
            status_code=409,
            detail="Цей кубик уже є у гравців. Вимкни його замість видалення.",
        )
    db.delete(die)
    db.commit()
    return finalized_admin_payload(db)
