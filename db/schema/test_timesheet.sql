--
-- PostgreSQL database dump
--

\restrict djtjnFhBOvRLvHdRq3vxu78ORLuEBumOh7yuzdd1FjNfLRPyWM8ww0VI9B7AsgZ

-- Dumped from database version 18.0
-- Dumped by pg_dump version 18.0

-- Started on 2026-10-22 12:11:36

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- TOC entry 6 (class 2615 OID 78744)
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- TOC entry 5735 (class 0 OID 0)
-- Dependencies: 6
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS '';


--
-- TOC entry 2 (class 3079 OID 79088)
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- TOC entry 5736 (class 0 OID 0)
-- Dependencies: 2
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';


--
-- TOC entry 362 (class 1255 OID 79814)
-- Name: fill_calendar_year(integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fill_calendar_year(p_year integer) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_work_id    INTEGER;
    v_weekend_id INTEGER;
BEGIN
    -- Заранее получаем ID из справочника, чтобы не выполнять SELECT в цикле для каждой строки
    SELECT id INTO v_work_id    FROM public.day_types WHERE code = 'WORK';
    SELECT id INTO v_weekend_id FROM public.day_types WHERE code = 'WEEKEND';

    INSERT INTO public.production_calendar (work_date, day_type_id)
    SELECT
        d::DATE,
        CASE
            WHEN EXTRACT(DOW FROM d) IN (0, 6) THEN v_weekend_id
            ELSE v_work_id
        END
    FROM generate_series(
        make_date(p_year, 1, 1),
        make_date(p_year, 12, 31),
        '1 day'::interval
    ) AS d
    ON CONFLICT (work_date) DO NOTHING;

    RAISE NOTICE 'Календарь на % год успешно сгенерирован с привязкой к day_types', p_year;
END;
$$;


--
-- TOC entry 419 (class 1255 OID 117404)
-- Name: trg_absence_displaces_fact(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_absence_displaces_fact() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Блокировка до DELETE: параллельная отметка факта этого инженера
    -- дождётся конца нашей транзакции и увидит неявку.
    PERFORM pg_advisory_xact_lock(7301, NEW.employee_id);

    -- Один оператор: удаление и запись в журнал либо проходят вместе,
    -- либо вместе откатываются вместе с сохранением неявки.
    WITH removed AS (
        DELETE FROM public.fact_calendar_cells AS f
        WHERE f.employee_id = NEW.employee_id
          AND f.work_date BETWEEN NEW.date_from AND NEW.date_to
        RETURNING f.id, f.employee_id, f.work_date, f.mark_set_id,
                  f.is_retro, f.created_at
    )
    INSERT INTO public.fact_change_log (
        fact_cell_id, employee_id, work_date, mark_set_id,
        is_retro, marked_at, action, absence_period_id, changed_by
    )
    SELECT id, employee_id, work_date, mark_set_id,
           is_retro, created_at, 'absence_override', NEW.id, NEW.created_by
    FROM removed;

    -- AFTER-триггер: возвращаемое значение игнорируется.
    RETURN NULL;
END;
$$;


--
-- TOC entry 447 (class 1255 OID 79817)
-- Name: trg_check_composition_validity(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_check_composition_validity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.marka_to_stage
        WHERE marka_type_id = NEW.marka_type_id
          AND stage_id      = NEW.stage_id
    ) THEN
        RAISE EXCEPTION
            'Марка/раздел (id=%) не допускается для стадии (id=%). '
            'Добавьте разрешение в таблицу marka_to_stage.',
            NEW.marka_type_id, NEW.stage_id;
    END IF;

    -- Проверяем компетенцию отдела
    IF NOT EXISTS (
        SELECT 1
        FROM public.department_markas
        WHERE department_id = NEW.department_id
          AND marka_type_id = NEW.marka_type_id
    ) THEN
        RAISE EXCEPTION
            'Отдел (id=%) не имеет компетенции выпускать марку/раздел (id=%). '
            'Добавьте запись в таблицу department_markas.',
            NEW.department_id, NEW.marka_type_id;
    END IF;

    RETURN NEW;
END;
$$;


--
-- TOC entry 379 (class 1255 OID 117406)
-- Name: trg_fact_check_absence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_fact_check_absence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_letter text;
BEGIN
    PERFORM pg_advisory_xact_lock(7301, NEW.employee_id);

    -- Форма daterange(...) @> дата совпадает с выражением индекса
    -- absence_periods_no_overlap (gist), поэтому поиск идёт по индексу.
    SELECT t.tab_letter
      INTO v_letter
      FROM public.absence_periods AS a
      JOIN public.absence_types AS t ON t.id = a.absence_type_id
     WHERE a.employee_id = NEW.employee_id
       AND daterange(a.date_from, a.date_to, '[]') @> NEW.work_date;

    IF FOUND THEN
        RAISE EXCEPTION 'День % закрыт табелем (%)', NEW.work_date, v_letter
            USING ERRCODE = 'UF001';
    END IF;

    -- Пересчёт только когда меняется суть отметки: новая запись или
    -- перенос на другой шифр/день. Правка override_reason администратором
    -- не должна превращать своевременную отметку в ретроввод.
    IF TG_OP = 'INSERT'
       OR NEW.mark_set_id IS DISTINCT FROM OLD.mark_set_id
       OR NEW.work_date IS DISTINCT FROM OLD.work_date
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
    THEN
        -- current_date считается в часовом поясе сессии (Europe/Moscow,
        -- UTC+3 — совпадает с Волгоградом).
        NEW.is_retro := current_date > NEW.work_date;
    ELSE
        -- Прочие правки флаг не трогают, даже если его пытались задать.
        NEW.is_retro := OLD.is_retro;
    END IF;

    RETURN NEW;
END;
$$;


--
-- TOC entry 486 (class 1255 OID 78746)
-- Name: update_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_timestamp() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
   NEW.updated_at := NOW();
   RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 255 (class 1259 OID 79818)
-- Name: absence_periods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.absence_periods (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    absence_type_id integer NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    created_by integer,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT absence_periods_range_check CHECK ((date_to >= date_from))
);


--
-- TOC entry 5737 (class 0 OID 0)
-- Dependencies: 255
-- Name: TABLE absence_periods; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.absence_periods IS 'Периоды отсутствий сотрудников (отпуск, больничный, декрет и т.п.) для табеля';


--
-- TOC entry 5738 (class 0 OID 0)
-- Dependencies: 255
-- Name: COLUMN absence_periods.date_from; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.date_from IS 'Первый день периода отсутствия (включительно)';


--
-- TOC entry 5739 (class 0 OID 0)
-- Dependencies: 255
-- Name: COLUMN absence_periods.date_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.date_to IS 'Последний день периода отсутствия (включительно)';


--
-- TOC entry 5740 (class 0 OID 0)
-- Dependencies: 255
-- Name: COLUMN absence_periods.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.created_by IS 'Пользователь (табельщица), внёсший период — для аудита';


--
-- TOC entry 256 (class 1259 OID 79828)
-- Name: absence_periods_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.absence_periods_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5741 (class 0 OID 0)
-- Dependencies: 256
-- Name: absence_periods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.absence_periods_id_seq OWNED BY public.absence_periods.id;


--
-- TOC entry 257 (class 1259 OID 79829)
-- Name: absence_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.absence_types (
    id integer NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(50) NOT NULL,
    tab_letter character varying(5) NOT NULL,
    blocks_planning boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    t13_code character varying(2)
);


--
-- TOC entry 5742 (class 0 OID 0)
-- Dependencies: 257
-- Name: TABLE absence_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.absence_types IS 'Справочник видов неявок (отсутствий) для табеля учёта рабочего времени';


--
-- TOC entry 5743 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.code IS 'Машинный стабильный код вида неявки — опора для логики, не зависит от буквы табеля';


--
-- TOC entry 5744 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.name IS 'Человекочитаемое название для интерфейса';


--
-- TOC entry 5745 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.tab_letter; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.tab_letter IS 'Буквенный код в сетке табеля и в Excel-выгрузке (стандарт Т-13)';


--
-- TOC entry 5746 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.blocks_planning; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.blocks_planning IS 'true = день недоступен для планирования; false = не влияет на доступность';


--
-- TOC entry 5747 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.is_active IS 'false = вид неявки выключен из использования, но сохранён для истории';


--
-- TOC entry 5748 (class 0 OID 0)
-- Dependencies: 257
-- Name: COLUMN absence_types.t13_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.t13_code IS 'Цифровой код вида неявки по форме Т-13 (постановление Госкомстата № 1 от 05.01.2004). varchar: ведущий ноль значим';


--
-- TOC entry 258 (class 1259 OID 79840)
-- Name: absence_types_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.absence_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5749 (class 0 OID 0)
-- Dependencies: 258
-- Name: absence_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.absence_types_id_seq OWNED BY public.absence_types.id;


--
-- TOC entry 301 (class 1259 OID 125583)
-- Name: admin_change_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_change_items (
    id integer NOT NULL,
    changeset_id integer NOT NULL,
    layer character varying(4) NOT NULL,
    work_date date NOT NULL,
    old_mark_set_id integer,
    new_mark_set_id integer,
    old_marka text,
    new_marka text,
    CONSTRAINT admin_change_items_is_change CHECK ((old_marka IS DISTINCT FROM new_marka)),
    CONSTRAINT admin_change_items_layer_check CHECK (((layer)::text = ANY ((ARRAY['plan'::character varying, 'fact'::character varying])::text[]))),
    CONSTRAINT admin_change_items_new_snapshot CHECK (((new_mark_set_id IS NULL) OR (new_marka IS NOT NULL))),
    CONSTRAINT admin_change_items_old_snapshot CHECK (((old_mark_set_id IS NULL) OR (old_marka IS NOT NULL)))
);


--
-- TOC entry 5750 (class 0 OID 0)
-- Dependencies: 301
-- Name: TABLE admin_change_items; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_change_items IS 'Изменённые ячейки правки администратора (diff): полоса, день, было → стало. Только итог сохранения, по строке на ячейку';


--
-- TOC entry 300 (class 1259 OID 125582)
-- Name: admin_change_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.admin_change_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_change_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 299 (class 1259 OID 125557)
-- Name: admin_changesets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_changesets (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    changed_by integer NOT NULL,
    comment text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT admin_changesets_comment_not_blank CHECK ((btrim(comment) <> ''::text))
);


--
-- TOC entry 5751 (class 0 OID 0)
-- Dependencies: 299
-- Name: TABLE admin_changesets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_changesets IS 'Правка администратора (как коммит): кто, когда, чьи данные, комментарий. Изменённые ячейки — admin_change_items';


--
-- TOC entry 298 (class 1259 OID 125556)
-- Name: admin_changesets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.admin_changesets ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_changesets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 220 (class 1259 OID 78747)
-- Name: building_workshops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.building_workshops (
    workshop_id integer NOT NULL,
    building_id integer NOT NULL,
    enterprise_id integer NOT NULL
);


--
-- TOC entry 221 (class 1259 OID 78753)
-- Name: buildings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildings (
    id integer NOT NULL,
    enterprise_id integer NOT NULL,
    building_name character varying(100) NOT NULL,
    area real
);


--
-- TOC entry 222 (class 1259 OID 78759)
-- Name: buildings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.buildings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5752 (class 0 OID 0)
-- Dependencies: 222
-- Name: buildings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.buildings_id_seq OWNED BY public.buildings.id;


--
-- TOC entry 259 (class 1259 OID 79841)
-- Name: day_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.day_types (
    id integer NOT NULL,
    code character varying(10) NOT NULL,
    name character varying(50) NOT NULL,
    is_working boolean NOT NULL
);


--
-- TOC entry 5753 (class 0 OID 0)
-- Dependencies: 259
-- Name: TABLE day_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.day_types IS 'Справочник типов дней производственного календаря';


--
-- TOC entry 5754 (class 0 OID 0)
-- Dependencies: 259
-- Name: COLUMN day_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.code IS 'Уникальный строковый код типа дня';


--
-- TOC entry 5755 (class 0 OID 0)
-- Dependencies: 259
-- Name: COLUMN day_types.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.name IS 'Наименование типа дня для интерфейса';


--
-- TOC entry 5756 (class 0 OID 0)
-- Dependencies: 259
-- Name: COLUMN day_types.is_working; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.is_working IS 'true = рабочий день, false = нерабочий день (выходной, праздник)';


--
-- TOC entry 260 (class 1259 OID 79848)
-- Name: day_types_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.day_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5757 (class 0 OID 0)
-- Dependencies: 260
-- Name: day_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.day_types_id_seq OWNED BY public.day_types.id;


--
-- TOC entry 261 (class 1259 OID 79849)
-- Name: department_markas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.department_markas (
    department_id integer NOT NULL,
    marka_type_id integer NOT NULL
);


--
-- TOC entry 5758 (class 0 OID 0)
-- Dependencies: 261
-- Name: TABLE department_markas; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.department_markas IS 'Компетенции отделов: какие марки/разделы закреплены за каждым бюро';


--
-- TOC entry 262 (class 1259 OID 79854)
-- Name: departments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.departments (
    id integer NOT NULL,
    code character varying(20) NOT NULL,
    name text NOT NULL,
    parent_id integer
);


--
-- TOC entry 5759 (class 0 OID 0)
-- Dependencies: 262
-- Name: TABLE departments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.departments IS 'Организационная структура (отделы, бюро, группы)';


--
-- TOC entry 5760 (class 0 OID 0)
-- Dependencies: 262
-- Name: COLUMN departments.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.departments.code IS 'Краткий код отдела (например, СТР, ТЕХН)';


--
-- TOC entry 5761 (class 0 OID 0)
-- Dependencies: 262
-- Name: COLUMN departments.parent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.departments.parent_id IS 'Ссылка на вышестоящее подразделение (для построения дерева)';


--
-- TOC entry 263 (class 1259 OID 79862)
-- Name: departments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.departments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5762 (class 0 OID 0)
-- Dependencies: 263
-- Name: departments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.departments_id_seq OWNED BY public.departments.id;


--
-- TOC entry 223 (class 1259 OID 78760)
-- Name: designer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.designer (
    id integer NOT NULL,
    full_name character varying(255) NOT NULL
);


--
-- TOC entry 224 (class 1259 OID 78765)
-- Name: designer_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.designer_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5763 (class 0 OID 0)
-- Dependencies: 224
-- Name: designer_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.designer_id_seq OWNED BY public.designer.id;


--
-- TOC entry 289 (class 1259 OID 84597)
-- Name: employee_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_assignments (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    department_id integer NOT NULL,
    position_id integer NOT NULL,
    valid_from date NOT NULL,
    valid_to date,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT employee_assignments_range_check CHECK (((valid_to IS NULL) OR (valid_to >= valid_from)))
);


--
-- TOC entry 5764 (class 0 OID 0)
-- Dependencies: 289
-- Name: TABLE employee_assignments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employee_assignments IS 'Кадровые назначения: с какой по какую дату работник занимает должность в подразделении. Приём = открытие первого, перевод = закрытие+открытие, увольнение = закрытие последнего';


--
-- TOC entry 5765 (class 0 OID 0)
-- Dependencies: 289
-- Name: COLUMN employee_assignments.valid_from; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_assignments.valid_from IS 'Первый день действия назначения (дата приёма/перевода)';


--
-- TOC entry 5766 (class 0 OID 0)
-- Dependencies: 289
-- Name: COLUMN employee_assignments.valid_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_assignments.valid_to IS 'Последний день действия (дата увольнения/перевода). NULL = действует';


--
-- TOC entry 288 (class 1259 OID 84596)
-- Name: employee_assignments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.employee_assignments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5767 (class 0 OID 0)
-- Dependencies: 288
-- Name: employee_assignments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_assignments_id_seq OWNED BY public.employee_assignments.id;


--
-- TOC entry 294 (class 1259 OID 84712)
-- Name: employee_norms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employee_norms (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    reason_id integer NOT NULL,
    base_hours smallint NOT NULL,
    applies_short_day boolean DEFAULT true NOT NULL,
    reduced_dow smallint,
    reduced_hours smallint,
    valid_from date NOT NULL,
    valid_to date,
    created_by integer,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT employee_norms_base_hours_check CHECK (((base_hours >= 1) AND (base_hours <= 12))),
    CONSTRAINT employee_norms_reduced_dow_check CHECK (((reduced_dow IS NULL) OR ((reduced_dow >= 1) AND (reduced_dow <= 7)))),
    CONSTRAINT employee_norms_reduced_hours_check CHECK (((reduced_hours IS NULL) OR ((reduced_hours >= 1) AND (reduced_hours <= 12)))),
    CONSTRAINT employee_norms_reduced_less_check CHECK (((reduced_hours IS NULL) OR (reduced_hours < base_hours))),
    CONSTRAINT employee_norms_reduced_pair_check CHECK (((reduced_dow IS NULL) = (reduced_hours IS NULL))),
    CONSTRAINT employee_norms_valid_range_check CHECK (((valid_to IS NULL) OR (valid_to >= valid_from)))
);


--
-- TOC entry 5768 (class 0 OID 0)
-- Dependencies: 294
-- Name: TABLE employee_norms; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employee_norms IS 'Индивидуальная норма рабочего времени по правовому основанию. Версионируется датами; хранится только для отклонений от нормы предприятия (8 ч)';


--
-- TOC entry 5769 (class 0 OID 0)
-- Dependencies: 294
-- Name: COLUMN employee_norms.base_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.base_hours IS 'Снимок часов обычного дня на момент назначения (не ссылка на справочник)';


--
-- TOC entry 5770 (class 0 OID 0)
-- Dependencies: 294
-- Name: COLUMN employee_norms.reduced_dow; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.reduced_dow IS 'День недели с сокращённой продолжительностью (1=Пн … 7=Вс), NULL если основание его не предполагает';


--
-- TOC entry 5771 (class 0 OID 0)
-- Dependencies: 294
-- Name: COLUMN employee_norms.reduced_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.reduced_hours IS 'Часы в сокращённый день недели. Заполняется в паре с reduced_dow';


--
-- TOC entry 5772 (class 0 OID 0)
-- Dependencies: 294
-- Name: COLUMN employee_norms.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.created_by IS 'Пользователь, заведший версию нормы — для аудита';


--
-- TOC entry 293 (class 1259 OID 84711)
-- Name: employee_norms_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.employee_norms_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5773 (class 0 OID 0)
-- Dependencies: 293
-- Name: employee_norms_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_norms_id_seq OWNED BY public.employee_norms.id;


--
-- TOC entry 264 (class 1259 OID 79880)
-- Name: employees; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.employees (
    id integer NOT NULL,
    user_id integer,
    personnel_number character varying(20) NOT NULL,
    full_name character varying(100) NOT NULL,
    room character varying(10),
    phone character varying(20),
    CONSTRAINT employees_personnel_number_digits CHECK (((personnel_number)::text ~ '^[0-9]+$'::text))
);


--
-- TOC entry 5774 (class 0 OID 0)
-- Dependencies: 264
-- Name: TABLE employees; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employees IS 'Работник как личность (стабильная запись). Отдел, должность и период работы — в employee_assignments';


--
-- TOC entry 5775 (class 0 OID 0)
-- Dependencies: 264
-- Name: COLUMN employees.personnel_number; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.personnel_number IS 'Табельный номер для бухгалтерского табеля. Обязателен и уникален';


--
-- TOC entry 5776 (class 0 OID 0)
-- Dependencies: 264
-- Name: COLUMN employees.room; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.room IS 'Номер кабинета. Справочное поле, история переездов не ведётся';


--
-- TOC entry 5777 (class 0 OID 0)
-- Dependencies: 264
-- Name: COLUMN employees.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.phone IS 'Внутренний телефон. Не уникален: номер закреплён за рабочим местом и может быть общим для нескольких работников либо передан другому при увольнении';


--
-- TOC entry 265 (class 1259 OID 79889)
-- Name: employees_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.employees_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5778 (class 0 OID 0)
-- Dependencies: 265
-- Name: employees_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employees_id_seq OWNED BY public.employees.id;


--
-- TOC entry 225 (class 1259 OID 78766)
-- Name: enterprises; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enterprises (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    city character varying(100)
);


--
-- TOC entry 226 (class 1259 OID 78771)
-- Name: enterprises_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.enterprises_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5779 (class 0 OID 0)
-- Dependencies: 226
-- Name: enterprises_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.enterprises_id_seq OWNED BY public.enterprises.id;


--
-- TOC entry 266 (class 1259 OID 79890)
-- Name: fact_calendar_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_calendar_cells (
    id integer NOT NULL,
    mark_set_id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    is_auto boolean DEFAULT false NOT NULL,
    override_reason text,
    created_at timestamp with time zone DEFAULT now(),
    is_retro boolean DEFAULT false NOT NULL
);


--
-- TOC entry 5780 (class 0 OID 0)
-- Dependencies: 266
-- Name: TABLE fact_calendar_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_calendar_cells IS 'Фактические рабочие дни инженера. Автономна — не знает про Канбан';


--
-- TOC entry 5781 (class 0 OID 0)
-- Dependencies: 266
-- Name: COLUMN fact_calendar_cells.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.work_date IS 'FK на production_calendar — нельзя отметить факт на несуществующий или выходной день';


--
-- TOC entry 5782 (class 0 OID 0)
-- Dependencies: 266
-- Name: COLUMN fact_calendar_cells.is_auto; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.is_auto IS 'false = инженер нажал вручную; true = система создала автоматически через Канбан';


--
-- TOC entry 5783 (class 0 OID 0)
-- Dependencies: 266
-- Name: COLUMN fact_calendar_cells.override_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.override_reason IS 'Причина отклонения от плана (например: «переброска по заданию руководства»)';


--
-- TOC entry 5784 (class 0 OID 0)
-- Dependencies: 266
-- Name: COLUMN fact_calendar_cells.is_retro; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.is_retro IS 'true = отметка поставлена или перенесена после окончания work_date. Вычисляется триггером trg_fact_check_absence, из приложения не задаётся';


--
-- TOC entry 267 (class 1259 OID 79902)
-- Name: fact_calendar_cells_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fact_calendar_cells_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5785 (class 0 OID 0)
-- Dependencies: 267
-- Name: fact_calendar_cells_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fact_calendar_cells_id_seq OWNED BY public.fact_calendar_cells.id;


--
-- TOC entry 297 (class 1259 OID 117369)
-- Name: fact_change_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_change_log (
    id integer NOT NULL,
    fact_cell_id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    mark_set_id integer,
    is_retro boolean NOT NULL,
    marked_at timestamp with time zone,
    action character varying(30) NOT NULL,
    absence_period_id integer,
    changed_by integer,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fact_change_log_action_check CHECK (((action)::text = 'absence_override'::text))
);


--
-- TOC entry 5786 (class 0 OID 0)
-- Dependencies: 297
-- Name: TABLE fact_change_log; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_change_log IS 'Журнал фактов, удалённых из fact_calendar_cells: кем, когда, почему. Позволяет администратору восстановить факт (например, после снятия НН)';


--
-- TOC entry 296 (class 1259 OID 117368)
-- Name: fact_change_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.fact_change_log ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.fact_change_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- TOC entry 268 (class 1259 OID 79903)
-- Name: fact_kanban_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_kanban_cells (
    fact_cell_id integer NOT NULL,
    kanban_task_id integer NOT NULL,
    linked_at timestamp with time zone DEFAULT now()
);


--
-- TOC entry 5787 (class 0 OID 0)
-- Dependencies: 268
-- Name: TABLE fact_kanban_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_kanban_cells IS 'Расширение факта для Канбана. Связывает ячейку факта с конкретной канбан-карточкой. Удаление этой таблицы не затрагивает fact_calendar_cells';


--
-- TOC entry 5788 (class 0 OID 0)
-- Dependencies: 268
-- Name: COLUMN fact_kanban_cells.fact_cell_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_kanban_cells.fact_cell_id IS 'Один к одному с fact_calendar_cells: одна ячейка факта — одна карточка';


--
-- TOC entry 227 (class 1259 OID 78772)
-- Name: gips; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gips (
    id integer NOT NULL,
    full_name character varying(255) NOT NULL,
    "position" character varying(100) DEFAULT 'Главный инженер проекта'::character varying
);


--
-- TOC entry 228 (class 1259 OID 78778)
-- Name: gips_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gips_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5789 (class 0 OID 0)
-- Dependencies: 228
-- Name: gips_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gips_id_seq OWNED BY public.gips.id;


--
-- TOC entry 269 (class 1259 OID 79909)
-- Name: kanban_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.kanban_tasks (
    id integer NOT NULL,
    mark_set_id integer NOT NULL,
    title text NOT NULL,
    status character varying(20) DEFAULT 'backlog'::character varying NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    weight_days integer,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT kanban_tasks_status_check CHECK (((status)::text = ANY (ARRAY[('backlog'::character varying)::text, ('in_progress'::character varying)::text, ('done'::character varying)::text]))),
    CONSTRAINT kanban_tasks_weight_days_check CHECK ((weight_days > 0))
);


--
-- TOC entry 5790 (class 0 OID 0)
-- Dependencies: 269
-- Name: TABLE kanban_tasks; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.kanban_tasks IS 'Подзадачи инженера (Канбан-карточки). Сейчас не используются — заглушка для масштабирования';


--
-- TOC entry 5791 (class 0 OID 0)
-- Dependencies: 269
-- Name: COLUMN kanban_tasks.weight_days; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.weight_days IS 'Оценка инженера: сколько рабочих дней займёт карточка';


--
-- TOC entry 5792 (class 0 OID 0)
-- Dependencies: 269
-- Name: COLUMN kanban_tasks.started_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.started_at IS 'Автоматически: момент перевода карточки в in_progress';


--
-- TOC entry 5793 (class 0 OID 0)
-- Dependencies: 269
-- Name: COLUMN kanban_tasks.completed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.completed_at IS 'Автоматически: момент перевода карточки в done';


--
-- TOC entry 270 (class 1259 OID 79924)
-- Name: kanban_tasks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.kanban_tasks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5794 (class 0 OID 0)
-- Dependencies: 270
-- Name: kanban_tasks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kanban_tasks_id_seq OWNED BY public.kanban_tasks.id;


--
-- TOC entry 271 (class 1259 OID 79925)
-- Name: marka_to_stage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marka_to_stage (
    marka_type_id integer NOT NULL,
    stage_id integer NOT NULL
);


--
-- TOC entry 5795 (class 0 OID 0)
-- Dependencies: 271
-- Name: TABLE marka_to_stage; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.marka_to_stage IS 'Матрица допустимости: какие марки/разделы разрешены для конкретных стадий';


--
-- TOC entry 272 (class 1259 OID 79930)
-- Name: marka_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marka_types (
    id integer NOT NULL,
    code character varying(20) NOT NULL,
    name text NOT NULL
);


--
-- TOC entry 5796 (class 0 OID 0)
-- Dependencies: 272
-- Name: TABLE marka_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.marka_types IS 'Справочник типов марок/разделов (АР, КЖ, ТХ, ОПЗ, ИОС и т.д.)';


--
-- TOC entry 5797 (class 0 OID 0)
-- Dependencies: 272
-- Name: COLUMN marka_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.marka_types.code IS 'Буквенный код марки или раздела по стандарту';


--
-- TOC entry 273 (class 1259 OID 79938)
-- Name: marka_types_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.marka_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5798 (class 0 OID 0)
-- Dependencies: 273
-- Name: marka_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.marka_types_id_seq OWNED BY public.marka_types.id;


--
-- TOC entry 292 (class 1259 OID 84651)
-- Name: norm_reasons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.norm_reasons (
    id integer NOT NULL,
    code character varying(30) NOT NULL,
    name character varying(100) NOT NULL,
    legal_ref text,
    base_hours smallint NOT NULL,
    has_reduced_day boolean DEFAULT false NOT NULL,
    applies_short_day boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT norm_reasons_base_hours_check CHECK (((base_hours >= 1) AND (base_hours <= 12)))
);


--
-- TOC entry 5799 (class 0 OID 0)
-- Dependencies: 292
-- Name: TABLE norm_reasons; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.norm_reasons IS 'Основания сокращённой продолжительности рабочего дня (инвалидность, многодетность и т.п.)';


--
-- TOC entry 5800 (class 0 OID 0)
-- Dependencies: 292
-- Name: COLUMN norm_reasons.legal_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.legal_ref IS 'Ссылка на норму права (статья ТК, коллективный договор, приказ). В расчёте не участвует';


--
-- TOC entry 5801 (class 0 OID 0)
-- Dependencies: 292
-- Name: COLUMN norm_reasons.base_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.base_hours IS 'Часы обычного рабочего дня по этому основанию — значение по умолчанию при вводе';


--
-- TOC entry 5802 (class 0 OID 0)
-- Dependencies: 292
-- Name: COLUMN norm_reasons.has_reduced_day; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.has_reduced_day IS 'true = основание предполагает один сокращённый день в неделю по выбору работника';


--
-- TOC entry 5803 (class 0 OID 0)
-- Dependencies: 292
-- Name: COLUMN norm_reasons.applies_short_day; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.applies_short_day IS 'Применяется ли предпраздничное сокращение (ст. 95 ТК РФ) к этому основанию';


--
-- TOC entry 291 (class 1259 OID 84650)
-- Name: norm_reasons_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.norm_reasons_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5804 (class 0 OID 0)
-- Dependencies: 291
-- Name: norm_reasons_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.norm_reasons_id_seq OWNED BY public.norm_reasons.id;


--
-- TOC entry 229 (class 1259 OID 78779)
-- Name: permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.permissions (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    description text
);


--
-- TOC entry 230 (class 1259 OID 78786)
-- Name: permissions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.permissions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5805 (class 0 OID 0)
-- Dependencies: 230
-- Name: permissions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.permissions_id_seq OWNED BY public.permissions.id;


--
-- TOC entry 274 (class 1259 OID 79939)
-- Name: plan_calendar_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_calendar_cells (
    id integer NOT NULL,
    work_plan_id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- TOC entry 5806 (class 0 OID 0)
-- Dependencies: 274
-- Name: TABLE plan_calendar_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.plan_calendar_cells IS 'Запланированные рабочие дни: одна строка = один день одного инженера на одном шифре';


--
-- TOC entry 5807 (class 0 OID 0)
-- Dependencies: 274
-- Name: COLUMN plan_calendar_cells.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.plan_calendar_cells.work_date IS 'FK на production_calendar гарантирует что дата существует и не является несуществующей';


--
-- TOC entry 275 (class 1259 OID 79947)
-- Name: plan_calendar_cells_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.plan_calendar_cells_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5808 (class 0 OID 0)
-- Dependencies: 275
-- Name: plan_calendar_cells_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.plan_calendar_cells_id_seq OWNED BY public.plan_calendar_cells.id;


--
-- TOC entry 276 (class 1259 OID 79948)
-- Name: positions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.positions (
    id integer NOT NULL,
    name text NOT NULL,
    rank integer NOT NULL
);


--
-- TOC entry 5809 (class 0 OID 0)
-- Dependencies: 276
-- Name: TABLE positions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.positions IS 'Справочник должностей с приоритетом';


--
-- TOC entry 5810 (class 0 OID 0)
-- Dependencies: 276
-- Name: COLUMN positions.rank; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.positions.rank IS 'Числовой ранг для логики согласования (чем меньше — тем выше статус)';


--
-- TOC entry 277 (class 1259 OID 79956)
-- Name: positions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.positions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5811 (class 0 OID 0)
-- Dependencies: 277
-- Name: positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.positions_id_seq OWNED BY public.positions.id;


--
-- TOC entry 278 (class 1259 OID 79957)
-- Name: production_calendar; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_calendar (
    work_date date NOT NULL,
    day_type_id integer NOT NULL,
    description text
);


--
-- TOC entry 5812 (class 0 OID 0)
-- Dependencies: 278
-- Name: TABLE production_calendar; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.production_calendar IS 'Производственный календарь России (нормализованный)';


--
-- TOC entry 5813 (class 0 OID 0)
-- Dependencies: 278
-- Name: COLUMN production_calendar.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.work_date IS 'Дата';


--
-- TOC entry 5814 (class 0 OID 0)
-- Dependencies: 278
-- Name: COLUMN production_calendar.day_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.day_type_id IS 'Ссылка на справочник типов дней day_types';


--
-- TOC entry 5815 (class 0 OID 0)
-- Dependencies: 278
-- Name: COLUMN production_calendar.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.description IS 'Описание праздника или причины переноса';


--
-- TOC entry 231 (class 1259 OID 78787)
-- Name: project_buildings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_buildings (
    project_id integer NOT NULL,
    building_id integer NOT NULL
);


--
-- TOC entry 279 (class 1259 OID 79964)
-- Name: project_composition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_composition (
    id integer NOT NULL,
    project_id integer NOT NULL,
    department_id integer NOT NULL,
    marka_type_id integer NOT NULL,
    stage_id integer NOT NULL
);


--
-- TOC entry 5816 (class 0 OID 0)
-- Dependencies: 279
-- Name: TABLE project_composition; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_composition IS 'Состав проекта: задание ГИПа — какой отдел, какую марку/раздел, на какой стадии';


--
-- TOC entry 5817 (class 0 OID 0)
-- Dependencies: 279
-- Name: COLUMN project_composition.stage_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_composition.stage_id IS 'Стадия, определённая ГИПом. Совместно с marka_type_id задаёт правила для документов';


--
-- TOC entry 280 (class 1259 OID 79972)
-- Name: project_composition_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_composition_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5818 (class 0 OID 0)
-- Dependencies: 280
-- Name: project_composition_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_composition_id_seq OWNED BY public.project_composition.id;


--
-- TOC entry 281 (class 1259 OID 79973)
-- Name: project_mark_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_mark_sets (
    id integer NOT NULL,
    composition_id integer NOT NULL,
    marka_full text NOT NULL,
    id_executor integer,
    status_id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    last_update timestamp with time zone,
    recipient_composition_id integer,
    CONSTRAINT project_mark_sets_recipient_not_self CHECK (((recipient_composition_id IS NULL) OR (recipient_composition_id <> composition_id)))
);


--
-- TOC entry 5819 (class 0 OID 0)
-- Dependencies: 281
-- Name: TABLE project_mark_sets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_mark_sets IS 'Реестр физических документов (комплектов РД и разделов ПД), созданных начальником бюро в рамках состава проекта';


--
-- TOC entry 5820 (class 0 OID 0)
-- Dependencies: 281
-- Name: COLUMN project_mark_sets.composition_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.composition_id IS 'Ссылка на строку состава проекта (задание ГИПа). Несколько документов могут ссылаться на один composition_id';


--
-- TOC entry 5821 (class 0 OID 0)
-- Dependencies: 281
-- Name: COLUMN project_mark_sets.marka_full; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.marka_full IS 'Полный уникальный шифр документа, вводится вручную (например, 053-22-14а-ТХ1, 053-22-14а-ОПЗ#1)';


--
-- TOC entry 5822 (class 0 OID 0)
-- Dependencies: 281
-- Name: COLUMN project_mark_sets.recipient_composition_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.recipient_composition_id IS 'Строка состава получателя задания. NULL — обычный комплект; заполнено — задание смежному отделу. Признак задания выводится из этой колонки, отдельного флага нет.';


--
-- TOC entry 282 (class 1259 OID 79983)
-- Name: project_mark_sets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_mark_sets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5823 (class 0 OID 0)
-- Dependencies: 282
-- Name: project_mark_sets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_mark_sets_id_seq OWNED BY public.project_mark_sets.id;


--
-- TOC entry 232 (class 1259 OID 78792)
-- Name: project_register; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_register (
    project_id integer NOT NULL,
    registration_date date DEFAULT CURRENT_DATE NOT NULL,
    gip_id integer,
    doc_id integer,
    doc_link text,
    basis_document_text text,
    basis_document_link text,
    note text,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 233 (class 1259 OID 78801)
-- Name: project_registry_view; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.project_registry_view AS
SELECT
    NULL::integer AS register_id,
    NULL::date AS registration_date,
    NULL::text AS note,
    NULL::integer AS register_doc_id,
    NULL::text AS register_doc_link,
    NULL::timestamp without time zone AS register_updated_at,
    NULL::integer AS project_id,
    NULL::character varying(100) AS project_code,
    NULL::text AS project_code_link,
    NULL::text AS project_title,
    NULL::character varying(50) AS stage_name,
    NULL::character varying(255) AS enterprise_name,
    NULL::character varying(100) AS workshop_name,
    NULL::text AS building_names,
    NULL::text AS customer_display_name,
    NULL::character varying(255) AS gip_full_name,
    NULL::character varying(255) AS designer_full_name,
    NULL::character varying(50) AS project_status,
    NULL::timestamp without time zone AS project_created_at,
    NULL::timestamp without time zone AS project_updated_at;


--
-- TOC entry 234 (class 1259 OID 78805)
-- Name: project_stages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_stages (
    id integer NOT NULL,
    stage_name character varying(50) NOT NULL
);


--
-- TOC entry 235 (class 1259 OID 78810)
-- Name: project_stages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_stages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5824 (class 0 OID 0)
-- Dependencies: 235
-- Name: project_stages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_stages_id_seq OWNED BY public.project_stages.id;


--
-- TOC entry 236 (class 1259 OID 78811)
-- Name: project_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_status (
    id integer NOT NULL,
    status_name character varying(50) NOT NULL,
    description text
);


--
-- TOC entry 237 (class 1259 OID 78818)
-- Name: project_status_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_status_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5825 (class 0 OID 0)
-- Dependencies: 237
-- Name: project_status_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_status_id_seq OWNED BY public.project_status.id;


--
-- TOC entry 238 (class 1259 OID 78819)
-- Name: project_status_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_status_log (
    id integer NOT NULL,
    project_id integer NOT NULL,
    old_status_id integer,
    new_status_id integer NOT NULL,
    comment text NOT NULL,
    changed_by integer,
    changed_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 239 (class 1259 OID 78829)
-- Name: project_status_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.project_status_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5826 (class 0 OID 0)
-- Dependencies: 239
-- Name: project_status_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_status_log_id_seq OWNED BY public.project_status_log.id;


--
-- TOC entry 240 (class 1259 OID 78830)
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id integer NOT NULL,
    code character varying(100) NOT NULL,
    code_link text,
    title text NOT NULL,
    enterprise_id integer NOT NULL,
    workshop_id integer,
    stage_id integer,
    designer_id integer,
    status_id integer DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 241 (class 1259 OID 78842)
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying(50) NOT NULL,
    password_hash character varying(255) NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- TOC entry 242 (class 1259 OID 78851)
-- Name: project_status_log_view; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.project_status_log_view AS
 SELECT l.id AS log_id,
    p.code AS project_code,
    p.title AS project_title,
    ps_old.status_name AS old_status_name,
    ps_new.status_name AS new_status_name,
    l.comment,
    (u.username)::character varying(100) AS changed_by_name,
    l.changed_at
   FROM ((((public.project_status_log l
     JOIN public.projects p ON ((l.project_id = p.id)))
     LEFT JOIN public.project_status ps_old ON ((l.old_status_id = ps_old.id)))
     LEFT JOIN public.project_status ps_new ON ((l.new_status_id = ps_new.id)))
     LEFT JOIN public.users u ON ((l.changed_by = u.id)))
  ORDER BY l.changed_at DESC;


--
-- TOC entry 243 (class 1259 OID 78856)
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5827 (class 0 OID 0)
-- Dependencies: 243
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- TOC entry 244 (class 1259 OID 78857)
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permissions (
    role_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- TOC entry 245 (class 1259 OID 78862)
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    description text
);


--
-- TOC entry 246 (class 1259 OID 78869)
-- Name: roles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.roles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5828 (class 0 OID 0)
-- Dependencies: 246
-- Name: roles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.roles_id_seq OWNED BY public.roles.id;


--
-- TOC entry 283 (class 1259 OID 79984)
-- Name: set_statuses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.set_statuses (
    id integer NOT NULL,
    name text NOT NULL,
    color character varying(20),
    sort_order integer DEFAULT 0
);


--
-- TOC entry 5829 (class 0 OID 0)
-- Dependencies: 283
-- Name: TABLE set_statuses; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.set_statuses IS 'Жизненный цикл документа: В плане, В работе, На проверке, Выпущен…';


--
-- TOC entry 284 (class 1259 OID 79992)
-- Name: set_statuses_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.set_statuses_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5830 (class 0 OID 0)
-- Dependencies: 284
-- Name: set_statuses_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.set_statuses_id_seq OWNED BY public.set_statuses.id;


--
-- TOC entry 247 (class 1259 OID 78870)
-- Name: test; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.test (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    city character varying(100)
);


--
-- TOC entry 248 (class 1259 OID 78875)
-- Name: test_child; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.test_child (
    id integer NOT NULL,
    test_id integer NOT NULL,
    description character varying(255)
);


--
-- TOC entry 249 (class 1259 OID 78880)
-- Name: test_child_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.test_child_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5831 (class 0 OID 0)
-- Dependencies: 249
-- Name: test_child_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.test_child_id_seq OWNED BY public.test_child.id;


--
-- TOC entry 250 (class 1259 OID 78881)
-- Name: test_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.test_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5832 (class 0 OID 0)
-- Dependencies: 250
-- Name: test_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.test_id_seq OWNED BY public.test.id;


--
-- TOC entry 251 (class 1259 OID 78882)
-- Name: user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_roles (
    user_id integer NOT NULL,
    role_id integer NOT NULL
);


--
-- TOC entry 252 (class 1259 OID 78887)
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5833 (class 0 OID 0)
-- Dependencies: 252
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- TOC entry 285 (class 1259 OID 79993)
-- Name: work_plan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_plan (
    id integer NOT NULL,
    mark_set_id integer NOT NULL,
    employee_id integer NOT NULL,
    start_date date NOT NULL,
    work_days integer NOT NULL,
    end_date date,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone,
    CONSTRAINT work_plan_status_check CHECK (((status)::text = ANY (ARRAY[('draft'::character varying)::text, ('confirmed'::character varying)::text, ('archived'::character varying)::text]))),
    CONSTRAINT work_plan_work_days_check CHECK ((work_days > 0))
);


--
-- TOC entry 5834 (class 0 OID 0)
-- Dependencies: 285
-- Name: TABLE work_plan; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.work_plan IS 'Задание начальника бюро: шифр + исполнитель + даты + кол-во дней';


--
-- TOC entry 5835 (class 0 OID 0)
-- Dependencies: 285
-- Name: COLUMN work_plan.start_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.work_plan.start_date IS 'Дата начала работы — вводит начальник бюро';


--
-- TOC entry 5836 (class 0 OID 0)
-- Dependencies: 285
-- Name: COLUMN work_plan.work_days; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.work_plan.work_days IS 'Количество рабочих дней — вводит начальник бюро';


--
-- TOC entry 5837 (class 0 OID 0)
-- Dependencies: 285
-- Name: COLUMN work_plan.end_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.work_plan.end_date IS 'Дата окончания — вычисляется триггером как MAX(work_date) из plan_calendar_cells';


--
-- TOC entry 5838 (class 0 OID 0)
-- Dependencies: 285
-- Name: COLUMN work_plan.status; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.work_plan.status IS 'draft=черновик (дни не заняты), confirmed=подтверждён, archived=архив';


--
-- TOC entry 290 (class 1259 OID 84633)
-- Name: v_calendar_view; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_calendar_view AS
 WITH plan_cte AS (
         SELECT pcc.employee_id,
            pcc.work_date,
            wp.id AS work_plan_id,
            wp.mark_set_id,
            pms.marka_full,
            p.code AS project_code,
            mt.code AS marka_code
           FROM (((((public.plan_calendar_cells pcc
             JOIN public.work_plan wp ON ((wp.id = pcc.work_plan_id)))
             JOIN public.project_mark_sets pms ON ((pms.id = wp.mark_set_id)))
             JOIN public.project_composition pc ON ((pc.id = pms.composition_id)))
             JOIN public.projects p ON ((p.id = pc.project_id)))
             JOIN public.marka_types mt ON ((mt.id = pc.marka_type_id)))
        ), fact_cte AS (
         SELECT fcc.id AS fact_cell_id,
            fcc.employee_id,
            fcc.work_date,
            fcc.mark_set_id,
            fcc.is_auto,
            fcc.override_reason,
            pms.marka_full,
            fkc.kanban_task_id,
            kt.title AS kanban_title,
            kt.status AS kanban_status,
            kt.weight_days AS kanban_weight_days
           FROM (((public.fact_calendar_cells fcc
             JOIN public.project_mark_sets pms ON ((pms.id = fcc.mark_set_id)))
             LEFT JOIN public.fact_kanban_cells fkc ON ((fkc.fact_cell_id = fcc.id)))
             LEFT JOIN public.kanban_tasks kt ON ((kt.id = fkc.kanban_task_id)))
        )
 SELECT COALESCE(pl.employee_id, fa.employee_id) AS employee_id,
    e.full_name AS employee_name,
    d.code AS department_code,
    COALESCE(pl.work_date, fa.work_date) AS work_date,
    dt.code AS calendar_day_type,
    pl.work_plan_id,
    pl.mark_set_id AS plan_mark_set_id,
    pl.marka_full AS plan_marka_full,
    pl.project_code AS plan_project_code,
    pl.marka_code AS plan_marka_code,
    fa.fact_cell_id,
    fa.mark_set_id AS fact_mark_set_id,
    fa.marka_full AS fact_marka_full,
    fa.is_auto,
    fa.override_reason,
    fa.kanban_task_id,
    fa.kanban_title,
    fa.kanban_status,
    fa.kanban_weight_days,
        CASE
            WHEN ((pl.mark_set_id IS NOT NULL) AND (fa.mark_set_id IS NOT NULL) AND (pl.mark_set_id = fa.mark_set_id)) THEN 'done'::text
            WHEN ((pl.mark_set_id IS NOT NULL) AND (fa.mark_set_id IS NULL)) THEN 'plan'::text
            WHEN ((pl.mark_set_id IS NOT NULL) AND (fa.mark_set_id IS NOT NULL) AND (pl.mark_set_id <> fa.mark_set_id)) THEN 'deviation'::text
            WHEN ((pl.mark_set_id IS NULL) AND (fa.mark_set_id IS NOT NULL)) THEN 'unplanned'::text
            ELSE 'free'::text
        END AS cell_status
   FROM ((((((plan_cte pl
     FULL JOIN fact_cte fa ON (((fa.employee_id = pl.employee_id) AND (fa.work_date = pl.work_date))))
     JOIN public.employees e ON ((e.id = COALESCE(pl.employee_id, fa.employee_id))))
     LEFT JOIN public.employee_assignments ea ON (((ea.employee_id = e.id) AND (COALESCE(pl.work_date, fa.work_date) >= ea.valid_from) AND ((ea.valid_to IS NULL) OR (COALESCE(pl.work_date, fa.work_date) <= ea.valid_to)))))
     LEFT JOIN public.departments d ON ((d.id = ea.department_id)))
     LEFT JOIN public.production_calendar pc_cal ON ((pc_cal.work_date = COALESCE(pl.work_date, fa.work_date))))
     LEFT JOIN public.day_types dt ON ((dt.id = pc_cal.day_type_id)));


--
-- TOC entry 5839 (class 0 OID 0)
-- Dependencies: 290
-- Name: VIEW v_calendar_view; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_calendar_view IS 'Календарь план/факт для интерфейса планирования. Статусы ячеек: done / plan / deviation / unplanned / free';


--
-- TOC entry 286 (class 1259 OID 80011)
-- Name: v_project_composition; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_project_composition AS
SELECT
    NULL::integer AS composition_id,
    NULL::character varying(100) AS project_code,
    NULL::character varying(20) AS department_code,
    NULL::text AS department_name,
    NULL::text AS parent_department_name,
    NULL::character varying(20) AS marka_code,
    NULL::text AS marka_name,
    NULL::character varying(50) AS stage_name,
    NULL::bigint AS documents_count,
    NULL::integer AS project_id,
    NULL::integer AS marka_type_id,
    NULL::integer AS department_id,
    NULL::integer AS stage_id;


--
-- TOC entry 5840 (class 0 OID 0)
-- Dependencies: 286
-- Name: VIEW v_project_composition; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_project_composition IS 'Состав проекта с иерархией отделов и счётчиком физических документов';


--
-- TOC entry 295 (class 1259 OID 84750)
-- Name: v_timesheet; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_timesheet AS
 SELECT e.id AS employee_id,
    e.full_name AS employee_name,
    e.personnel_number,
    d.code AS department_code,
    pc.work_date,
    dt.code AS day_type_code,
    dt.is_working,
        CASE
            WHEN (ap.id IS NOT NULL) THEN 'absence'::text
            WHEN (dt.is_working = false) THEN 'nonwork'::text
            ELSE 'work'::text
        END AS cell_kind,
        CASE
            WHEN ((ap.id IS NULL) AND (dt.is_working = true)) THEN (COALESCE((
            CASE
                WHEN ((en.reduced_dow IS NOT NULL) AND (EXTRACT(isodow FROM pc.work_date) = (en.reduced_dow)::numeric)) THEN en.reduced_hours
                ELSE en.base_hours
            END)::integer, 8) -
            CASE
                WHEN (((dt.code)::text = 'SHORT'::text) AND COALESCE(en.applies_short_day, true)) THEN 1
                ELSE 0
            END)
            ELSE NULL::integer
        END AS hours,
    at.tab_letter AS absence_letter,
    at.code AS absence_code
   FROM (((((((public.employees e
     CROSS JOIN public.production_calendar pc)
     JOIN public.day_types dt ON ((dt.id = pc.day_type_id)))
     JOIN public.employee_assignments ea ON (((ea.employee_id = e.id) AND (pc.work_date >= ea.valid_from) AND ((ea.valid_to IS NULL) OR (pc.work_date <= ea.valid_to)))))
     JOIN public.departments d ON ((d.id = ea.department_id)))
     LEFT JOIN public.absence_periods ap ON (((ap.employee_id = e.id) AND (pc.work_date >= ap.date_from) AND (pc.work_date <= ap.date_to))))
     LEFT JOIN public.absence_types at ON ((at.id = ap.absence_type_id)))
     LEFT JOIN public.employee_norms en ON (((en.employee_id = e.id) AND (pc.work_date >= en.valid_from) AND ((en.valid_to IS NULL) OR (pc.work_date <= en.valid_to)))));


--
-- TOC entry 5841 (class 0 OID 0)
-- Dependencies: 295
-- Name: VIEW v_timesheet; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_timesheet IS 'Табель на презумпции явки: рабочий день = отработан по норме, если не накрыт периодом неявки. Часы считаются от employee_norms, от fact_calendar_cells не зависит';


--
-- TOC entry 287 (class 1259 OID 80020)
-- Name: work_plan_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.work_plan_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5842 (class 0 OID 0)
-- Dependencies: 287
-- Name: work_plan_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.work_plan_id_seq OWNED BY public.work_plan.id;


--
-- TOC entry 253 (class 1259 OID 78888)
-- Name: workshops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workshops (
    id integer NOT NULL,
    enterprise_id integer NOT NULL,
    workshop_name character varying(100) NOT NULL
);


--
-- TOC entry 254 (class 1259 OID 78894)
-- Name: workshops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workshops_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- TOC entry 5843 (class 0 OID 0)
-- Dependencies: 254
-- Name: workshops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workshops_id_seq OWNED BY public.workshops.id;


--
-- TOC entry 5277 (class 2604 OID 80021)
-- Name: absence_periods id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods ALTER COLUMN id SET DEFAULT nextval('public.absence_periods_id_seq'::regclass);


--
-- TOC entry 5279 (class 2604 OID 80022)
-- Name: absence_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types ALTER COLUMN id SET DEFAULT nextval('public.absence_types_id_seq'::regclass);


--
-- TOC entry 5254 (class 2604 OID 78895)
-- Name: buildings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings ALTER COLUMN id SET DEFAULT nextval('public.buildings_id_seq'::regclass);


--
-- TOC entry 5282 (class 2604 OID 80023)
-- Name: day_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types ALTER COLUMN id SET DEFAULT nextval('public.day_types_id_seq'::regclass);


--
-- TOC entry 5283 (class 2604 OID 80024)
-- Name: departments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments ALTER COLUMN id SET DEFAULT nextval('public.departments_id_seq'::regclass);


--
-- TOC entry 5255 (class 2604 OID 78896)
-- Name: designer id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer ALTER COLUMN id SET DEFAULT nextval('public.designer_id_seq'::regclass);


--
-- TOC entry 5306 (class 2604 OID 84600)
-- Name: employee_assignments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments ALTER COLUMN id SET DEFAULT nextval('public.employee_assignments_id_seq'::regclass);


--
-- TOC entry 5312 (class 2604 OID 84715)
-- Name: employee_norms id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms ALTER COLUMN id SET DEFAULT nextval('public.employee_norms_id_seq'::regclass);


--
-- TOC entry 5284 (class 2604 OID 80026)
-- Name: employees id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees ALTER COLUMN id SET DEFAULT nextval('public.employees_id_seq'::regclass);


--
-- TOC entry 5256 (class 2604 OID 78897)
-- Name: enterprises id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises ALTER COLUMN id SET DEFAULT nextval('public.enterprises_id_seq'::regclass);


--
-- TOC entry 5285 (class 2604 OID 80027)
-- Name: fact_calendar_cells id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells ALTER COLUMN id SET DEFAULT nextval('public.fact_calendar_cells_id_seq'::regclass);


--
-- TOC entry 5257 (class 2604 OID 78898)
-- Name: gips id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips ALTER COLUMN id SET DEFAULT nextval('public.gips_id_seq'::regclass);


--
-- TOC entry 5290 (class 2604 OID 80028)
-- Name: kanban_tasks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks ALTER COLUMN id SET DEFAULT nextval('public.kanban_tasks_id_seq'::regclass);


--
-- TOC entry 5294 (class 2604 OID 80029)
-- Name: marka_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_types ALTER COLUMN id SET DEFAULT nextval('public.marka_types_id_seq'::regclass);


--
-- TOC entry 5308 (class 2604 OID 84654)
-- Name: norm_reasons id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons ALTER COLUMN id SET DEFAULT nextval('public.norm_reasons_id_seq'::regclass);


--
-- TOC entry 5259 (class 2604 OID 78899)
-- Name: permissions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);


--
-- TOC entry 5295 (class 2604 OID 80030)
-- Name: plan_calendar_cells id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells ALTER COLUMN id SET DEFAULT nextval('public.plan_calendar_cells_id_seq'::regclass);


--
-- TOC entry 5297 (class 2604 OID 80031)
-- Name: positions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions ALTER COLUMN id SET DEFAULT nextval('public.positions_id_seq'::regclass);


--
-- TOC entry 5298 (class 2604 OID 80032)
-- Name: project_composition id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition ALTER COLUMN id SET DEFAULT nextval('public.project_composition_id_seq'::regclass);


--
-- TOC entry 5299 (class 2604 OID 80033)
-- Name: project_mark_sets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets ALTER COLUMN id SET DEFAULT nextval('public.project_mark_sets_id_seq'::regclass);


--
-- TOC entry 5262 (class 2604 OID 78900)
-- Name: project_stages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages ALTER COLUMN id SET DEFAULT nextval('public.project_stages_id_seq'::regclass);


--
-- TOC entry 5263 (class 2604 OID 78901)
-- Name: project_status id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status ALTER COLUMN id SET DEFAULT nextval('public.project_status_id_seq'::regclass);


--
-- TOC entry 5264 (class 2604 OID 78902)
-- Name: project_status_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log ALTER COLUMN id SET DEFAULT nextval('public.project_status_log_id_seq'::regclass);


--
-- TOC entry 5266 (class 2604 OID 78903)
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- TOC entry 5273 (class 2604 OID 78904)
-- Name: roles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles ALTER COLUMN id SET DEFAULT nextval('public.roles_id_seq'::regclass);


--
-- TOC entry 5301 (class 2604 OID 80034)
-- Name: set_statuses id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.set_statuses ALTER COLUMN id SET DEFAULT nextval('public.set_statuses_id_seq'::regclass);


--
-- TOC entry 5274 (class 2604 OID 78905)
-- Name: test id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test ALTER COLUMN id SET DEFAULT nextval('public.test_id_seq'::regclass);


--
-- TOC entry 5275 (class 2604 OID 78906)
-- Name: test_child id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child ALTER COLUMN id SET DEFAULT nextval('public.test_child_id_seq'::regclass);


--
-- TOC entry 5270 (class 2604 OID 78907)
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- TOC entry 5303 (class 2604 OID 80035)
-- Name: work_plan id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_plan ALTER COLUMN id SET DEFAULT nextval('public.work_plan_id_seq'::regclass);


--
-- TOC entry 5276 (class 2604 OID 78908)
-- Name: workshops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops ALTER COLUMN id SET DEFAULT nextval('public.workshops_id_seq'::regclass);


--
-- TOC entry 5406 (class 2606 OID 80037)
-- Name: absence_periods absence_periods_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(date_from, date_to, '[]'::text) WITH &&);


--
-- TOC entry 5408 (class 2606 OID 80039)
-- Name: absence_periods absence_periods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_pkey PRIMARY KEY (id);


--
-- TOC entry 5411 (class 2606 OID 80041)
-- Name: absence_types absence_types_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_code_key UNIQUE (code);


--
-- TOC entry 5413 (class 2606 OID 80043)
-- Name: absence_types absence_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_pkey PRIMARY KEY (id);


--
-- TOC entry 5415 (class 2606 OID 84756)
-- Name: absence_types absence_types_t13_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_t13_code_key UNIQUE (t13_code);


--
-- TOC entry 5417 (class 2606 OID 80045)
-- Name: absence_types absence_types_tab_letter_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_tab_letter_key UNIQUE (tab_letter);


--
-- TOC entry 5505 (class 2606 OID 125597)
-- Name: admin_change_items admin_change_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_pkey PRIMARY KEY (id);


--
-- TOC entry 5507 (class 2606 OID 125599)
-- Name: admin_change_items admin_change_items_unique_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_unique_cell UNIQUE (changeset_id, layer, work_date);


--
-- TOC entry 5502 (class 2606 OID 125570)
-- Name: admin_changesets admin_changesets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_pkey PRIMARY KEY (id);


--
-- TOC entry 5339 (class 2606 OID 78910)
-- Name: building_workshops building_workshops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT building_workshops_pkey PRIMARY KEY (workshop_id, building_id);


--
-- TOC entry 5341 (class 2606 OID 78912)
-- Name: buildings buildings_id_enterprise_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_id_enterprise_id_key UNIQUE (id, enterprise_id);


--
-- TOC entry 5343 (class 2606 OID 78914)
-- Name: buildings buildings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_pkey PRIMARY KEY (id);


--
-- TOC entry 5419 (class 2606 OID 80047)
-- Name: day_types day_types_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types
    ADD CONSTRAINT day_types_code_key UNIQUE (code);


--
-- TOC entry 5421 (class 2606 OID 80049)
-- Name: day_types day_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types
    ADD CONSTRAINT day_types_pkey PRIMARY KEY (id);


--
-- TOC entry 5423 (class 2606 OID 80051)
-- Name: department_markas department_markas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_pkey PRIMARY KEY (department_id, marka_type_id);


--
-- TOC entry 5425 (class 2606 OID 80053)
-- Name: departments departments_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_code_key UNIQUE (code);


--
-- TOC entry 5427 (class 2606 OID 80055)
-- Name: departments departments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_pkey PRIMARY KEY (id);


--
-- TOC entry 5345 (class 2606 OID 78916)
-- Name: designer designer_full_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer
    ADD CONSTRAINT designer_full_name_key UNIQUE (full_name);


--
-- TOC entry 5347 (class 2606 OID 78918)
-- Name: designer designer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer
    ADD CONSTRAINT designer_pkey PRIMARY KEY (id);


--
-- TOC entry 5484 (class 2606 OID 84611)
-- Name: employee_assignments employee_assignments_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(valid_from, valid_to, '[]'::text) WITH &&);


--
-- TOC entry 5486 (class 2606 OID 84609)
-- Name: employee_assignments employee_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_pkey PRIMARY KEY (id);


--
-- TOC entry 5494 (class 2606 OID 84733)
-- Name: employee_norms employee_norms_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(valid_from, valid_to, '[]'::text) WITH &&);


--
-- TOC entry 5496 (class 2606 OID 84731)
-- Name: employee_norms employee_norms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_pkey PRIMARY KEY (id);


--
-- TOC entry 5430 (class 2606 OID 84630)
-- Name: employees employees_personnel_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_personnel_number_key UNIQUE (personnel_number);


--
-- TOC entry 5432 (class 2606 OID 80061)
-- Name: employees employees_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_pkey PRIMARY KEY (id);


--
-- TOC entry 5434 (class 2606 OID 84632)
-- Name: employees employees_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_user_id_key UNIQUE (user_id);


--
-- TOC entry 5349 (class 2606 OID 78920)
-- Name: enterprises enterprises_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises
    ADD CONSTRAINT enterprises_name_key UNIQUE (name);


--
-- TOC entry 5351 (class 2606 OID 78922)
-- Name: enterprises enterprises_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises
    ADD CONSTRAINT enterprises_pkey PRIMARY KEY (id);


--
-- TOC entry 5436 (class 2606 OID 80063)
-- Name: fact_calendar_cells fact_calendar_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_pkey PRIMARY KEY (id);


--
-- TOC entry 5499 (class 2606 OID 117382)
-- Name: fact_change_log fact_change_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_pkey PRIMARY KEY (id);


--
-- TOC entry 5442 (class 2606 OID 80065)
-- Name: fact_kanban_cells fact_kanban_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_pkey PRIMARY KEY (fact_cell_id);


--
-- TOC entry 5353 (class 2606 OID 78924)
-- Name: gips gips_full_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips
    ADD CONSTRAINT gips_full_name_key UNIQUE (full_name);


--
-- TOC entry 5355 (class 2606 OID 78926)
-- Name: gips gips_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips
    ADD CONSTRAINT gips_pkey PRIMARY KEY (id);


--
-- TOC entry 5446 (class 2606 OID 80067)
-- Name: kanban_tasks kanban_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks
    ADD CONSTRAINT kanban_tasks_pkey PRIMARY KEY (id);


--
-- TOC entry 5448 (class 2606 OID 80069)
-- Name: marka_to_stage marka_to_stage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_pkey PRIMARY KEY (marka_type_id, stage_id);


--
-- TOC entry 5451 (class 2606 OID 80071)
-- Name: marka_types marka_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_types
    ADD CONSTRAINT marka_types_pkey PRIMARY KEY (id);


--
-- TOC entry 5490 (class 2606 OID 84671)
-- Name: norm_reasons norm_reasons_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons
    ADD CONSTRAINT norm_reasons_code_key UNIQUE (code);


--
-- TOC entry 5492 (class 2606 OID 84669)
-- Name: norm_reasons norm_reasons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons
    ADD CONSTRAINT norm_reasons_pkey PRIMARY KEY (id);


--
-- TOC entry 5357 (class 2606 OID 78928)
-- Name: permissions permissions_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_name_key UNIQUE (name);


--
-- TOC entry 5359 (class 2606 OID 78930)
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- TOC entry 5455 (class 2606 OID 80073)
-- Name: plan_calendar_cells plan_calendar_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_pkey PRIMARY KEY (id);


--
-- TOC entry 5459 (class 2606 OID 84649)
-- Name: positions positions_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_name_key UNIQUE (name);


--
-- TOC entry 5461 (class 2606 OID 80075)
-- Name: positions positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_pkey PRIMARY KEY (id);


--
-- TOC entry 5463 (class 2606 OID 80077)
-- Name: production_calendar production_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_calendar
    ADD CONSTRAINT production_calendar_pkey PRIMARY KEY (work_date);


--
-- TOC entry 5361 (class 2606 OID 78932)
-- Name: project_buildings project_buildings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT project_buildings_pkey PRIMARY KEY (project_id, building_id);


--
-- TOC entry 5466 (class 2606 OID 80079)
-- Name: project_composition project_composition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_pkey PRIMARY KEY (id);


--
-- TOC entry 5474 (class 2606 OID 80081)
-- Name: project_mark_sets project_mark_sets_marka_full_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_marka_full_key UNIQUE (marka_full);


--
-- TOC entry 5476 (class 2606 OID 80083)
-- Name: project_mark_sets project_mark_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_pkey PRIMARY KEY (id);


--
-- TOC entry 5363 (class 2606 OID 78934)
-- Name: project_register project_register_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_pkey PRIMARY KEY (project_id);


--
-- TOC entry 5365 (class 2606 OID 78936)
-- Name: project_stages project_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages
    ADD CONSTRAINT project_stages_pkey PRIMARY KEY (id);


--
-- TOC entry 5367 (class 2606 OID 78938)
-- Name: project_stages project_stages_stage_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages
    ADD CONSTRAINT project_stages_stage_name_key UNIQUE (stage_name);


--
-- TOC entry 5374 (class 2606 OID 78940)
-- Name: project_status_log project_status_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_pkey PRIMARY KEY (id);


--
-- TOC entry 5369 (class 2606 OID 78942)
-- Name: project_status project_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status
    ADD CONSTRAINT project_status_pkey PRIMARY KEY (id);


--
-- TOC entry 5371 (class 2606 OID 78944)
-- Name: project_status project_status_status_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status
    ADD CONSTRAINT project_status_status_name_key UNIQUE (status_name);


--
-- TOC entry 5376 (class 2606 OID 78946)
-- Name: projects projects_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_code_key UNIQUE (code);


--
-- TOC entry 5378 (class 2606 OID 78948)
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- TOC entry 5380 (class 2606 OID 78950)
-- Name: projects projects_unique_pair; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_unique_pair UNIQUE (id, enterprise_id);


--
-- TOC entry 5386 (class 2606 OID 78952)
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- TOC entry 5388 (class 2606 OID 78954)
-- Name: roles roles_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_name_key UNIQUE (name);


--
-- TOC entry 5390 (class 2606 OID 78956)
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- TOC entry 5478 (class 2606 OID 80085)
-- Name: set_statuses set_statuses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.set_statuses
    ADD CONSTRAINT set_statuses_pkey PRIMARY KEY (id);


--
-- TOC entry 5396 (class 2606 OID 78958)
-- Name: test_child test_child_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child
    ADD CONSTRAINT test_child_pkey PRIMARY KEY (id);


--
-- TOC entry 5392 (class 2606 OID 78960)
-- Name: test test_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test
    ADD CONSTRAINT test_name_key UNIQUE (name);


--
-- TOC entry 5394 (class 2606 OID 78962)
-- Name: test test_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test
    ADD CONSTRAINT test_pkey PRIMARY KEY (id);


--
-- TOC entry 5468 (class 2606 OID 80087)
-- Name: project_composition unique_composition; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT unique_composition UNIQUE (project_id, department_id, marka_type_id, stage_id);


--
-- TOC entry 5440 (class 2606 OID 80089)
-- Name: fact_calendar_cells unique_fact_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT unique_fact_cell UNIQUE (employee_id, work_date);


--
-- TOC entry 5457 (class 2606 OID 80091)
-- Name: plan_calendar_cells unique_plan_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT unique_plan_cell UNIQUE (employee_id, work_date);


--
-- TOC entry 5398 (class 2606 OID 78964)
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (user_id, role_id);


--
-- TOC entry 5382 (class 2606 OID 78966)
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- TOC entry 5384 (class 2606 OID 78968)
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- TOC entry 5482 (class 2606 OID 80095)
-- Name: work_plan work_plan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_plan
    ADD CONSTRAINT work_plan_pkey PRIMARY KEY (id);


--
-- TOC entry 5400 (class 2606 OID 78970)
-- Name: workshops workshops_enterprise_id_workshop_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_enterprise_id_workshop_name_key UNIQUE (enterprise_id, workshop_name);


--
-- TOC entry 5402 (class 2606 OID 78972)
-- Name: workshops workshops_id_enterprise_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_id_enterprise_id_key UNIQUE (id, enterprise_id);


--
-- TOC entry 5404 (class 2606 OID 78974)
-- Name: workshops workshops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_pkey PRIMARY KEY (id);


--
-- TOC entry 5409 (class 1259 OID 80096)
-- Name: idx_absence_periods_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_absence_periods_employee ON public.absence_periods USING btree (employee_id, date_from);


--
-- TOC entry 5503 (class 1259 OID 125581)
-- Name: idx_admin_changesets_employee_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_changesets_employee_created ON public.admin_changesets USING btree (employee_id, created_at DESC);


--
-- TOC entry 5487 (class 1259 OID 84628)
-- Name: idx_assignments_dept; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assignments_dept ON public.employee_assignments USING btree (department_id);


--
-- TOC entry 5488 (class 1259 OID 84627)
-- Name: idx_assignments_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assignments_employee ON public.employee_assignments USING btree (employee_id, valid_from);


--
-- TOC entry 5464 (class 1259 OID 80097)
-- Name: idx_composition_dept_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_composition_dept_stage ON public.project_composition USING btree (department_id, stage_id);


--
-- TOC entry 5428 (class 1259 OID 80098)
-- Name: idx_departments_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_departments_parent ON public.departments USING btree (parent_id);


--
-- TOC entry 5497 (class 1259 OID 84749)
-- Name: idx_employee_norms_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_employee_norms_lookup ON public.employee_norms USING btree (employee_id, valid_from);


--
-- TOC entry 5437 (class 1259 OID 80100)
-- Name: idx_fact_cells_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_cells_employee_date ON public.fact_calendar_cells USING btree (employee_id, work_date);


--
-- TOC entry 5438 (class 1259 OID 80101)
-- Name: idx_fact_cells_mark_set; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_cells_mark_set ON public.fact_calendar_cells USING btree (mark_set_id);


--
-- TOC entry 5500 (class 1259 OID 117403)
-- Name: idx_fact_change_log_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_change_log_employee_date ON public.fact_change_log USING btree (employee_id, work_date);


--
-- TOC entry 5443 (class 1259 OID 80102)
-- Name: idx_fact_kanban_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_kanban_task ON public.fact_kanban_cells USING btree (kanban_task_id);


--
-- TOC entry 5444 (class 1259 OID 80103)
-- Name: idx_kanban_mark_set; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kanban_mark_set ON public.kanban_tasks USING btree (mark_set_id);


--
-- TOC entry 5449 (class 1259 OID 80104)
-- Name: idx_marka_types_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_marka_types_code ON public.marka_types USING btree (code);


--
-- TOC entry 5479 (class 1259 OID 117364)
-- Name: idx_one_active_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_one_active_plan ON public.work_plan USING btree (mark_set_id, employee_id) WHERE ((status)::text = ANY ((ARRAY['draft'::character varying, 'confirmed'::character varying])::text[]));


--
-- TOC entry 5452 (class 1259 OID 80106)
-- Name: idx_plan_cells_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_cells_employee_date ON public.plan_calendar_cells USING btree (employee_id, work_date);


--
-- TOC entry 5453 (class 1259 OID 80107)
-- Name: idx_plan_cells_work_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_cells_work_plan ON public.plan_calendar_cells USING btree (work_plan_id);


--
-- TOC entry 5469 (class 1259 OID 80108)
-- Name: idx_sets_composition; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_composition ON public.project_mark_sets USING btree (composition_id);


--
-- TOC entry 5470 (class 1259 OID 80109)
-- Name: idx_sets_executor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_executor ON public.project_mark_sets USING btree (id_executor);


--
-- TOC entry 5471 (class 1259 OID 109188)
-- Name: idx_sets_recipient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_recipient ON public.project_mark_sets USING btree (recipient_composition_id) WHERE (recipient_composition_id IS NOT NULL);


--
-- TOC entry 5472 (class 1259 OID 80110)
-- Name: idx_sets_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_status ON public.project_mark_sets USING btree (status_id);


--
-- TOC entry 5372 (class 1259 OID 78975)
-- Name: idx_status_log_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_status_log_project ON public.project_status_log USING btree (project_id);


--
-- TOC entry 5480 (class 1259 OID 80111)
-- Name: idx_work_plan_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_plan_employee ON public.work_plan USING btree (employee_id);


--
-- TOC entry 5725 (class 2618 OID 78804)
-- Name: project_registry_view _RETURN; Type: RULE; Schema: public; Owner: -
--

CREATE OR REPLACE VIEW public.project_registry_view AS
 SELECT pr.project_id AS register_id,
    pr.registration_date,
    pr.note,
    pr.doc_id AS register_doc_id,
    pr.doc_link AS register_doc_link,
    pr.updated_at AS register_updated_at,
    p.id AS project_id,
    p.code AS project_code,
    p.code_link AS project_code_link,
    p.title AS project_title,
    ps.stage_name,
    e.name AS enterprise_name,
    w.workshop_name,
    string_agg((b.building_name)::text, ', '::text ORDER BY (b.building_name)::text) AS building_names,
    (((e.name)::text ||
        CASE
            WHEN (w.workshop_name IS NOT NULL) THEN (', цех '::text || (w.workshop_name)::text)
            ELSE ''::text
        END) ||
        CASE
            WHEN (count(b.id) = 1) THEN (', корпус '::text || string_agg((b.building_name)::text, ', '::text ORDER BY (b.building_name)::text))
            WHEN (count(b.id) > 1) THEN (', корпуса '::text || string_agg((b.building_name)::text, ', '::text ORDER BY (b.building_name)::text))
            ELSE ''::text
        END) AS customer_display_name,
    g.full_name AS gip_full_name,
    d.full_name AS designer_full_name,
    s.status_name AS project_status,
    p.created_at AS project_created_at,
    p.updated_at AS project_updated_at
   FROM (((((((((public.project_register pr
     JOIN public.projects p ON ((pr.project_id = p.id)))
     LEFT JOIN public.project_stages ps ON ((p.stage_id = ps.id)))
     LEFT JOIN public.enterprises e ON ((p.enterprise_id = e.id)))
     LEFT JOIN public.workshops w ON ((p.workshop_id = w.id)))
     LEFT JOIN public.project_buildings pb ON ((p.id = pb.project_id)))
     LEFT JOIN public.buildings b ON ((pb.building_id = b.id)))
     LEFT JOIN public.gips g ON ((pr.gip_id = g.id)))
     LEFT JOIN public.designer d ON ((p.designer_id = d.id)))
     LEFT JOIN public.project_status s ON ((p.status_id = s.id)))
  GROUP BY pr.project_id, p.id, ps.stage_name, e.name, w.workshop_name, g.full_name, d.full_name, s.status_name
  ORDER BY (split_part((p.code)::text, '-'::text, 2)), (split_part((p.code)::text, '-'::text, 1));


--
-- TOC entry 5727 (class 2618 OID 80014)
-- Name: v_project_composition _RETURN; Type: RULE; Schema: public; Owner: -
--

CREATE OR REPLACE VIEW public.v_project_composition AS
 SELECT pc.id AS composition_id,
    p.code AS project_code,
    d.code AS department_code,
    d.name AS department_name,
    d_parent.name AS parent_department_name,
    mt.code AS marka_code,
    mt.name AS marka_name,
    ps.stage_name,
    count(pms.id) AS documents_count,
    pc.project_id,
    pc.marka_type_id,
    pc.department_id,
    pc.stage_id
   FROM ((((((public.project_composition pc
     JOIN public.projects p ON ((p.id = pc.project_id)))
     JOIN public.departments d ON ((d.id = pc.department_id)))
     JOIN public.marka_types mt ON ((mt.id = pc.marka_type_id)))
     JOIN public.project_stages ps ON ((ps.id = pc.stage_id)))
     LEFT JOIN public.departments d_parent ON ((d_parent.id = d.parent_id)))
     LEFT JOIN public.project_mark_sets pms ON ((pms.composition_id = pc.id)))
  GROUP BY pc.id, p.code, d.code, d.name, d_parent.name, mt.code, mt.name, ps.stage_name;


--
-- TOC entry 5575 (class 2620 OID 117405)
-- Name: absence_periods trg_absence_displaces_fact; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_absence_displaces_fact AFTER INSERT OR UPDATE OF employee_id, date_from, date_to ON public.absence_periods FOR EACH ROW EXECUTE FUNCTION public.trg_absence_displaces_fact();


--
-- TOC entry 5577 (class 2620 OID 80113)
-- Name: project_composition trg_composition_validity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_composition_validity BEFORE INSERT OR UPDATE ON public.project_composition FOR EACH ROW EXECUTE FUNCTION public.trg_check_composition_validity();


--
-- TOC entry 5576 (class 2620 OID 117407)
-- Name: fact_calendar_cells trg_fact_check_absence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_fact_check_absence BEFORE INSERT OR UPDATE ON public.fact_calendar_cells FOR EACH ROW EXECUTE FUNCTION public.trg_fact_check_absence();


--
-- TOC entry 5574 (class 2620 OID 78977)
-- Name: projects update_projects_timestamp; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_projects_timestamp BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.update_timestamp();


--
-- TOC entry 5530 (class 2606 OID 80115)
-- Name: absence_periods absence_periods_absence_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_absence_type_id_fkey FOREIGN KEY (absence_type_id) REFERENCES public.absence_types(id) ON DELETE RESTRICT;


--
-- TOC entry 5531 (class 2606 OID 80120)
-- Name: absence_periods absence_periods_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- TOC entry 5532 (class 2606 OID 80125)
-- Name: absence_periods absence_periods_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5571 (class 2606 OID 125600)
-- Name: admin_change_items admin_change_items_changeset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_changeset_id_fkey FOREIGN KEY (changeset_id) REFERENCES public.admin_changesets(id) ON DELETE CASCADE;


--
-- TOC entry 5572 (class 2606 OID 125610)
-- Name: admin_change_items admin_change_items_new_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_new_mark_set_id_fkey FOREIGN KEY (new_mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE SET NULL;


--
-- TOC entry 5573 (class 2606 OID 125605)
-- Name: admin_change_items admin_change_items_old_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_old_mark_set_id_fkey FOREIGN KEY (old_mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE SET NULL;


--
-- TOC entry 5569 (class 2606 OID 125576)
-- Name: admin_changesets admin_changesets_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- TOC entry 5570 (class 2606 OID 125571)
-- Name: admin_changesets admin_changesets_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5510 (class 2606 OID 78978)
-- Name: buildings buildings_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5533 (class 2606 OID 80130)
-- Name: department_markas department_markas_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE CASCADE;


--
-- TOC entry 5534 (class 2606 OID 80135)
-- Name: department_markas department_markas_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE CASCADE;


--
-- TOC entry 5535 (class 2606 OID 80140)
-- Name: departments departments_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.departments(id) ON DELETE SET NULL;


--
-- TOC entry 5559 (class 2606 OID 84617)
-- Name: employee_assignments employee_assignments_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE RESTRICT;


--
-- TOC entry 5560 (class 2606 OID 84612)
-- Name: employee_assignments employee_assignments_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE CASCADE;


--
-- TOC entry 5561 (class 2606 OID 84622)
-- Name: employee_assignments employee_assignments_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.positions(id) ON DELETE RESTRICT;


--
-- TOC entry 5562 (class 2606 OID 84744)
-- Name: employee_norms employee_norms_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- TOC entry 5563 (class 2606 OID 84734)
-- Name: employee_norms employee_norms_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE CASCADE;


--
-- TOC entry 5564 (class 2606 OID 84739)
-- Name: employee_norms employee_norms_reason_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_reason_id_fkey FOREIGN KEY (reason_id) REFERENCES public.norm_reasons(id) ON DELETE RESTRICT;


--
-- TOC entry 5536 (class 2606 OID 84761)
-- Name: employees employees_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- TOC entry 5537 (class 2606 OID 80165)
-- Name: fact_calendar_cells fact_calendar_cells_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5538 (class 2606 OID 80170)
-- Name: fact_calendar_cells fact_calendar_cells_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE CASCADE;


--
-- TOC entry 5539 (class 2606 OID 80175)
-- Name: fact_calendar_cells fact_calendar_cells_work_date_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_work_date_fkey FOREIGN KEY (work_date) REFERENCES public.production_calendar(work_date);


--
-- TOC entry 5565 (class 2606 OID 117393)
-- Name: fact_change_log fact_change_log_absence_period_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_absence_period_id_fkey FOREIGN KEY (absence_period_id) REFERENCES public.absence_periods(id) ON DELETE SET NULL;


--
-- TOC entry 5566 (class 2606 OID 117398)
-- Name: fact_change_log fact_change_log_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- TOC entry 5567 (class 2606 OID 117383)
-- Name: fact_change_log fact_change_log_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5568 (class 2606 OID 117388)
-- Name: fact_change_log fact_change_log_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE SET NULL;


--
-- TOC entry 5540 (class 2606 OID 80180)
-- Name: fact_kanban_cells fact_kanban_cells_fact_cell_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_fact_cell_id_fkey FOREIGN KEY (fact_cell_id) REFERENCES public.fact_calendar_cells(id) ON DELETE CASCADE;


--
-- TOC entry 5541 (class 2606 OID 80185)
-- Name: fact_kanban_cells fact_kanban_cells_kanban_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_kanban_task_id_fkey FOREIGN KEY (kanban_task_id) REFERENCES public.kanban_tasks(id) ON DELETE CASCADE;


--
-- TOC entry 5508 (class 2606 OID 78983)
-- Name: building_workshops fk_bw_building; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT fk_bw_building FOREIGN KEY (building_id, enterprise_id) REFERENCES public.buildings(id, enterprise_id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5509 (class 2606 OID 78988)
-- Name: building_workshops fk_bw_workshop; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT fk_bw_workshop FOREIGN KEY (workshop_id, enterprise_id) REFERENCES public.workshops(id, enterprise_id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5511 (class 2606 OID 78993)
-- Name: project_buildings fk_project_buildings_building; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT fk_project_buildings_building FOREIGN KEY (building_id) REFERENCES public.buildings(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5512 (class 2606 OID 78998)
-- Name: project_buildings fk_project_buildings_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT fk_project_buildings_project FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5524 (class 2606 OID 79003)
-- Name: role_permissions fk_rp_permission; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT fk_rp_permission FOREIGN KEY (permission_id) REFERENCES public.permissions(id) ON DELETE CASCADE;


--
-- TOC entry 5525 (class 2606 OID 79008)
-- Name: role_permissions fk_rp_role; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT fk_rp_role FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- TOC entry 5527 (class 2606 OID 79013)
-- Name: user_roles fk_ur_role; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_ur_role FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- TOC entry 5528 (class 2606 OID 79018)
-- Name: user_roles fk_ur_user; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_ur_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- TOC entry 5542 (class 2606 OID 80190)
-- Name: kanban_tasks kanban_tasks_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks
    ADD CONSTRAINT kanban_tasks_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE CASCADE;


--
-- TOC entry 5543 (class 2606 OID 80195)
-- Name: marka_to_stage marka_to_stage_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE CASCADE;


--
-- TOC entry 5544 (class 2606 OID 80200)
-- Name: marka_to_stage marka_to_stage_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON DELETE CASCADE;


--
-- TOC entry 5545 (class 2606 OID 80205)
-- Name: plan_calendar_cells plan_calendar_cells_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5546 (class 2606 OID 80210)
-- Name: plan_calendar_cells plan_calendar_cells_work_date_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_work_date_fkey FOREIGN KEY (work_date) REFERENCES public.production_calendar(work_date);


--
-- TOC entry 5547 (class 2606 OID 80215)
-- Name: plan_calendar_cells plan_calendar_cells_work_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_work_plan_id_fkey FOREIGN KEY (work_plan_id) REFERENCES public.work_plan(id) ON DELETE CASCADE;


--
-- TOC entry 5548 (class 2606 OID 80220)
-- Name: production_calendar production_calendar_day_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_calendar
    ADD CONSTRAINT production_calendar_day_type_id_fkey FOREIGN KEY (day_type_id) REFERENCES public.day_types(id) ON DELETE RESTRICT;


--
-- TOC entry 5549 (class 2606 OID 80225)
-- Name: project_composition project_composition_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE RESTRICT;


--
-- TOC entry 5550 (class 2606 OID 80230)
-- Name: project_composition project_composition_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE RESTRICT;


--
-- TOC entry 5551 (class 2606 OID 80235)
-- Name: project_composition project_composition_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- TOC entry 5552 (class 2606 OID 80240)
-- Name: project_composition project_composition_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON DELETE RESTRICT;


--
-- TOC entry 5553 (class 2606 OID 109172)
-- Name: project_mark_sets project_mark_sets_composition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_composition_id_fkey FOREIGN KEY (composition_id) REFERENCES public.project_composition(id) ON DELETE RESTRICT;


--
-- TOC entry 5554 (class 2606 OID 80250)
-- Name: project_mark_sets project_mark_sets_id_executor_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_id_executor_fkey FOREIGN KEY (id_executor) REFERENCES public.employees(id) ON DELETE SET NULL;


--
-- TOC entry 5555 (class 2606 OID 109182)
-- Name: project_mark_sets project_mark_sets_recipient_composition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_recipient_composition_id_fkey FOREIGN KEY (recipient_composition_id) REFERENCES public.project_composition(id) ON DELETE RESTRICT;


--
-- TOC entry 5556 (class 2606 OID 80255)
-- Name: project_mark_sets project_mark_sets_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_status_id_fkey FOREIGN KEY (status_id) REFERENCES public.set_statuses(id) ON DELETE RESTRICT;


--
-- TOC entry 5513 (class 2606 OID 79023)
-- Name: project_register project_register_gip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_gip_id_fkey FOREIGN KEY (gip_id) REFERENCES public.gips(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5514 (class 2606 OID 79028)
-- Name: project_register project_register_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5515 (class 2606 OID 79033)
-- Name: project_status_log project_status_log_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5516 (class 2606 OID 79038)
-- Name: project_status_log project_status_log_new_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_new_status_id_fkey FOREIGN KEY (new_status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- TOC entry 5517 (class 2606 OID 79043)
-- Name: project_status_log project_status_log_old_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_old_status_id_fkey FOREIGN KEY (old_status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5518 (class 2606 OID 79048)
-- Name: project_status_log project_status_log_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5519 (class 2606 OID 79053)
-- Name: projects projects_designer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_designer_id_fkey FOREIGN KEY (designer_id) REFERENCES public.designer(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5520 (class 2606 OID 79058)
-- Name: projects projects_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- TOC entry 5521 (class 2606 OID 79063)
-- Name: projects projects_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5522 (class 2606 OID 79068)
-- Name: projects projects_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_status_id_fkey FOREIGN KEY (status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5523 (class 2606 OID 79073)
-- Name: projects projects_workshop_enterprise_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_workshop_enterprise_fkey FOREIGN KEY (workshop_id, enterprise_id) REFERENCES public.workshops(id, enterprise_id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- TOC entry 5526 (class 2606 OID 79078)
-- Name: test_child test_child_test_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child
    ADD CONSTRAINT test_child_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.test(id) ON DELETE CASCADE;


--
-- TOC entry 5557 (class 2606 OID 80260)
-- Name: work_plan work_plan_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_plan
    ADD CONSTRAINT work_plan_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- TOC entry 5558 (class 2606 OID 80265)
-- Name: work_plan work_plan_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_plan
    ADD CONSTRAINT work_plan_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE CASCADE;


--
-- TOC entry 5529 (class 2606 OID 79083)
-- Name: workshops workshops_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


-- Completed on 2026-10-22 12:11:36

--
-- PostgreSQL database dump complete
--

\unrestrict djtjnFhBOvRLvHdRq3vxu78ORLuEBumOh7yuzdd1FjNfLRPyWM8ww0VI9B7AsgZ

