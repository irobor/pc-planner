--
-- PostgreSQL database dump
--

\restrict 8fgSqLGUwbGE10k00bCscatAsEGiDWFjyqzKkAVFJORhlAG2ScdBv3vH7G9ZVRb

-- Dumped from database version 18.0
-- Dumped by pg_dump version 18.0

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
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS '';


--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;


--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';


--
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
        RETURNING f.id, f.employee_id, f.work_date, f.package_id,
                  f.is_retro, f.created_at
    )
    INSERT INTO public.fact_change_log (
        fact_cell_id, employee_id, work_date, package_id,
        is_retro, marked_at, action, absence_period_id, changed_by
    )
    SELECT id, employee_id, work_date, package_id,
           is_retro, created_at, 'absence_override', NEW.id, NEW.created_by
    FROM removed;

    -- AFTER-триггер: возвращаемое значение игнорируется.
    RETURN NULL;
END;
$$;


--
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
    -- перенос на другой пакет/день. Правка override_reason администратором
    -- не должна превращать своевременную отметку в ретроввод.
    IF TG_OP = 'INSERT'
       OR NEW.package_id IS DISTINCT FROM OLD.package_id
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
-- Name: TABLE absence_periods; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.absence_periods IS 'Периоды отсутствий сотрудников (отпуск, больничный, декрет и т.п.) для табеля';


--
-- Name: COLUMN absence_periods.date_from; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.date_from IS 'Первый день периода отсутствия (включительно)';


--
-- Name: COLUMN absence_periods.date_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.date_to IS 'Последний день периода отсутствия (включительно)';


--
-- Name: COLUMN absence_periods.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_periods.created_by IS 'Пользователь (табельщица), внёсший период — для аудита';


--
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
-- Name: absence_periods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.absence_periods_id_seq OWNED BY public.absence_periods.id;


--
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
-- Name: TABLE absence_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.absence_types IS 'Справочник видов неявок (отсутствий) для табеля учёта рабочего времени';


--
-- Name: COLUMN absence_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.code IS 'Машинный стабильный код вида неявки — опора для логики, не зависит от буквы табеля';


--
-- Name: COLUMN absence_types.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.name IS 'Человекочитаемое название для интерфейса';


--
-- Name: COLUMN absence_types.tab_letter; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.tab_letter IS 'Буквенный код в сетке табеля и в Excel-выгрузке (стандарт Т-13)';


--
-- Name: COLUMN absence_types.blocks_planning; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.blocks_planning IS 'true = день недоступен для планирования; false = не влияет на доступность';


--
-- Name: COLUMN absence_types.is_active; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.is_active IS 'false = вид неявки выключен из использования, но сохранён для истории';


--
-- Name: COLUMN absence_types.t13_code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.absence_types.t13_code IS 'Цифровой код вида неявки по форме Т-13 (постановление Госкомстата № 1 от 05.01.2004). varchar: ведущий ноль значим';


--
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
-- Name: absence_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.absence_types_id_seq OWNED BY public.absence_types.id;


--
-- Name: admin_change_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_change_items (
    id integer NOT NULL,
    changeset_id integer NOT NULL,
    layer character varying(4) NOT NULL,
    work_date date NOT NULL,
    old_marka text,
    new_marka text,
    old_package_id integer,
    new_package_id integer,
    CONSTRAINT admin_change_items_is_change CHECK ((old_marka IS DISTINCT FROM new_marka)),
    CONSTRAINT admin_change_items_layer_check CHECK (((layer)::text = ANY ((ARRAY['plan'::character varying, 'fact'::character varying])::text[]))),
    CONSTRAINT admin_change_items_new_snapshot CHECK (((new_package_id IS NULL) OR (new_marka IS NOT NULL))),
    CONSTRAINT admin_change_items_old_snapshot CHECK (((old_package_id IS NULL) OR (old_marka IS NOT NULL)))
);


--
-- Name: TABLE admin_change_items; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_change_items IS 'Изменённые ячейки правки администратора (diff): полоса, день, было → стало. Только итог сохранения, по строке на ячейку';


--
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
-- Name: TABLE admin_changesets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.admin_changesets IS 'Правка администратора (как коммит): кто, когда, чьи данные, комментарий. Изменённые ячейки — admin_change_items';


--
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
-- Name: building_workshops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.building_workshops (
    workshop_id integer NOT NULL,
    building_id integer NOT NULL,
    enterprise_id integer NOT NULL
);


--
-- Name: buildings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.buildings (
    id integer NOT NULL,
    enterprise_id integer NOT NULL,
    building_name character varying(100) NOT NULL,
    area real
);


--
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
-- Name: buildings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.buildings_id_seq OWNED BY public.buildings.id;


--
-- Name: day_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.day_types (
    id integer NOT NULL,
    code character varying(10) NOT NULL,
    name character varying(50) NOT NULL,
    is_working boolean NOT NULL
);


--
-- Name: TABLE day_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.day_types IS 'Справочник типов дней производственного календаря';


--
-- Name: COLUMN day_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.code IS 'Уникальный строковый код типа дня';


--
-- Name: COLUMN day_types.name; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.name IS 'Наименование типа дня для интерфейса';


--
-- Name: COLUMN day_types.is_working; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.day_types.is_working IS 'true = рабочий день, false = нерабочий день (выходной, праздник)';


--
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
-- Name: day_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.day_types_id_seq OWNED BY public.day_types.id;


--
-- Name: department_markas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.department_markas (
    department_id integer NOT NULL,
    marka_type_id integer NOT NULL
);


--
-- Name: TABLE department_markas; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.department_markas IS 'Компетенции отделов: какие марки/разделы закреплены за каждым бюро';


--
-- Name: departments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.departments (
    id integer NOT NULL,
    code character varying(20) NOT NULL,
    name text NOT NULL,
    parent_id integer
);


--
-- Name: TABLE departments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.departments IS 'Организационная структура (отделы, бюро, группы)';


--
-- Name: COLUMN departments.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.departments.code IS 'Краткий код отдела (например, СТР, ТЕХН)';


--
-- Name: COLUMN departments.parent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.departments.parent_id IS 'Ссылка на вышестоящее подразделение (для построения дерева)';


--
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
-- Name: departments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.departments_id_seq OWNED BY public.departments.id;


--
-- Name: designer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.designer (
    id integer NOT NULL,
    full_name character varying(255) NOT NULL
);


--
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
-- Name: designer_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.designer_id_seq OWNED BY public.designer.id;


--
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
-- Name: TABLE employee_assignments; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employee_assignments IS 'Кадровые назначения: с какой по какую дату работник занимает должность в подразделении. Приём = открытие первого, перевод = закрытие+открытие, увольнение = закрытие последнего';


--
-- Name: COLUMN employee_assignments.valid_from; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_assignments.valid_from IS 'Первый день действия назначения (дата приёма/перевода)';


--
-- Name: COLUMN employee_assignments.valid_to; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_assignments.valid_to IS 'Последний день действия (дата увольнения/перевода). NULL = действует';


--
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
-- Name: employee_assignments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_assignments_id_seq OWNED BY public.employee_assignments.id;


--
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
-- Name: TABLE employee_norms; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employee_norms IS 'Индивидуальная норма рабочего времени по правовому основанию. Версионируется датами; хранится только для отклонений от нормы предприятия (8 ч)';


--
-- Name: COLUMN employee_norms.base_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.base_hours IS 'Снимок часов обычного дня на момент назначения (не ссылка на справочник)';


--
-- Name: COLUMN employee_norms.reduced_dow; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.reduced_dow IS 'День недели с сокращённой продолжительностью (1=Пн … 7=Вс), NULL если основание его не предполагает';


--
-- Name: COLUMN employee_norms.reduced_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.reduced_hours IS 'Часы в сокращённый день недели. Заполняется в паре с reduced_dow';


--
-- Name: COLUMN employee_norms.created_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employee_norms.created_by IS 'Пользователь, заведший версию нормы — для аудита';


--
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
-- Name: employee_norms_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employee_norms_id_seq OWNED BY public.employee_norms.id;


--
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
-- Name: TABLE employees; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.employees IS 'Работник как личность (стабильная запись). Отдел, должность и период работы — в employee_assignments';


--
-- Name: COLUMN employees.personnel_number; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.personnel_number IS 'Табельный номер для бухгалтерского табеля. Обязателен и уникален';


--
-- Name: COLUMN employees.room; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.room IS 'Номер кабинета. Справочное поле, история переездов не ведётся';


--
-- Name: COLUMN employees.phone; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.employees.phone IS 'Внутренний телефон. Не уникален: номер закреплён за рабочим местом и может быть общим для нескольких работников либо передан другому при увольнении';


--
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
-- Name: employees_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.employees_id_seq OWNED BY public.employees.id;


--
-- Name: enterprises; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enterprises (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    city character varying(100)
);


--
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
-- Name: enterprises_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.enterprises_id_seq OWNED BY public.enterprises.id;


--
-- Name: fact_calendar_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_calendar_cells (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    is_auto boolean DEFAULT false NOT NULL,
    override_reason text,
    created_at timestamp with time zone DEFAULT now(),
    is_retro boolean DEFAULT false NOT NULL,
    package_id integer NOT NULL
);


--
-- Name: TABLE fact_calendar_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_calendar_cells IS 'Фактические рабочие дни инженера. Автономна — не знает про Канбан';


--
-- Name: COLUMN fact_calendar_cells.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.work_date IS 'FK на production_calendar — нельзя отметить факт на несуществующий или выходной день';


--
-- Name: COLUMN fact_calendar_cells.is_auto; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.is_auto IS 'false = инженер нажал вручную; true = система создала автоматически через Канбан';


--
-- Name: COLUMN fact_calendar_cells.override_reason; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.override_reason IS 'Причина отклонения от плана (например: «переброска по заданию руководства»)';


--
-- Name: COLUMN fact_calendar_cells.is_retro; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_calendar_cells.is_retro IS 'true = отметка поставлена или перенесена после окончания work_date. Вычисляется триггером trg_fact_check_absence, из приложения не задаётся';


--
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
-- Name: fact_calendar_cells_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fact_calendar_cells_id_seq OWNED BY public.fact_calendar_cells.id;


--
-- Name: fact_change_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_change_log (
    id integer NOT NULL,
    fact_cell_id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    is_retro boolean NOT NULL,
    marked_at timestamp with time zone,
    action character varying(30) NOT NULL,
    absence_period_id integer,
    changed_by integer,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    package_id integer,
    CONSTRAINT fact_change_log_action_check CHECK (((action)::text = 'absence_override'::text))
);


--
-- Name: TABLE fact_change_log; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_change_log IS 'Журнал фактов, удалённых из fact_calendar_cells: кем, когда, почему. Позволяет администратору восстановить факт (например, после снятия НН)';


--
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
-- Name: fact_kanban_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_kanban_cells (
    fact_cell_id integer NOT NULL,
    kanban_task_id integer NOT NULL,
    linked_at timestamp with time zone DEFAULT now()
);


--
-- Name: TABLE fact_kanban_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fact_kanban_cells IS 'Расширение факта для Канбана. Связывает ячейку факта с конкретной канбан-карточкой. Удаление этой таблицы не затрагивает fact_calendar_cells';


--
-- Name: COLUMN fact_kanban_cells.fact_cell_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.fact_kanban_cells.fact_cell_id IS 'Один к одному с fact_calendar_cells: одна ячейка факта — одна карточка';


--
-- Name: gips; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gips (
    id integer NOT NULL,
    full_name character varying(255) NOT NULL,
    "position" character varying(100) DEFAULT 'Главный инженер проекта'::character varying
);


--
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
-- Name: gips_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gips_id_seq OWNED BY public.gips.id;


--
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
-- Name: TABLE kanban_tasks; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.kanban_tasks IS 'Подзадачи инженера (Канбан-карточки). Сейчас не используются — заглушка для масштабирования';


--
-- Name: COLUMN kanban_tasks.weight_days; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.weight_days IS 'Оценка инженера: сколько рабочих дней займёт карточка';


--
-- Name: COLUMN kanban_tasks.started_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.started_at IS 'Автоматически: момент перевода карточки в in_progress';


--
-- Name: COLUMN kanban_tasks.completed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.kanban_tasks.completed_at IS 'Автоматически: момент перевода карточки в done';


--
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
-- Name: kanban_tasks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.kanban_tasks_id_seq OWNED BY public.kanban_tasks.id;


--
-- Name: marka_to_stage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marka_to_stage (
    marka_type_id integer NOT NULL,
    stage_id integer NOT NULL
);


--
-- Name: TABLE marka_to_stage; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.marka_to_stage IS 'Матрица допустимости: какие марки/разделы разрешены для конкретных стадий';


--
-- Name: marka_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.marka_types (
    id integer NOT NULL,
    code character varying(20) NOT NULL,
    name text NOT NULL
);


--
-- Name: TABLE marka_types; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.marka_types IS 'Справочник типов марок/разделов (АР, КЖ, ТХ, ОПЗ, ИОС и т.д.)';


--
-- Name: COLUMN marka_types.code; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.marka_types.code IS 'Буквенный код марки или раздела по стандарту';


--
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
-- Name: marka_types_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.marka_types_id_seq OWNED BY public.marka_types.id;


--
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
-- Name: TABLE norm_reasons; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.norm_reasons IS 'Основания сокращённой продолжительности рабочего дня (инвалидность, многодетность и т.п.)';


--
-- Name: COLUMN norm_reasons.legal_ref; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.legal_ref IS 'Ссылка на норму права (статья ТК, коллективный договор, приказ). В расчёте не участвует';


--
-- Name: COLUMN norm_reasons.base_hours; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.base_hours IS 'Часы обычного рабочего дня по этому основанию — значение по умолчанию при вводе';


--
-- Name: COLUMN norm_reasons.has_reduced_day; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.has_reduced_day IS 'true = основание предполагает один сокращённый день в неделю по выбору работника';


--
-- Name: COLUMN norm_reasons.applies_short_day; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.norm_reasons.applies_short_day IS 'Применяется ли предпраздничное сокращение (ст. 95 ТК РФ) к этому основанию';


--
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
-- Name: norm_reasons_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.norm_reasons_id_seq OWNED BY public.norm_reasons.id;


--
-- Name: package_statuses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.package_statuses (
    id integer NOT NULL,
    name text NOT NULL,
    note text,
    sort_order integer DEFAULT 0 NOT NULL,
    is_done boolean DEFAULT false NOT NULL
);


--
-- Name: TABLE package_statuses; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.package_statuses IS 'Статусы пакета работ. Ставит исполнитель пакета, включая стадии согласования';


--
-- Name: COLUMN package_statuses.is_done; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.package_statuses.is_done IS 'true — разработка пакета завершена; по таким пакетам считается готовность комплекта';


--
-- Name: permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.permissions (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    description text
);


--
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
-- Name: permissions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.permissions_id_seq OWNED BY public.permissions.id;


--
-- Name: plan_calendar_cells; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_calendar_cells (
    id integer NOT NULL,
    employee_id integer NOT NULL,
    work_date date NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    package_id integer NOT NULL
);


--
-- Name: TABLE plan_calendar_cells; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.plan_calendar_cells IS 'Запланированные рабочие дни: одна строка = один день одного инженера на одном пакете';


--
-- Name: COLUMN plan_calendar_cells.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.plan_calendar_cells.work_date IS 'FK на production_calendar гарантирует что дата существует и не является несуществующей';


--
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
-- Name: plan_calendar_cells_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.plan_calendar_cells_id_seq OWNED BY public.plan_calendar_cells.id;


--
-- Name: positions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.positions (
    id integer NOT NULL,
    name text NOT NULL,
    rank integer NOT NULL
);


--
-- Name: TABLE positions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.positions IS 'Справочник должностей с приоритетом';


--
-- Name: COLUMN positions.rank; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.positions.rank IS 'Числовой ранг для логики согласования (чем меньше — тем выше статус)';


--
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
-- Name: positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.positions_id_seq OWNED BY public.positions.id;


--
-- Name: production_calendar; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_calendar (
    work_date date NOT NULL,
    day_type_id integer NOT NULL,
    description text
);


--
-- Name: TABLE production_calendar; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.production_calendar IS 'Производственный календарь России (нормализованный)';


--
-- Name: COLUMN production_calendar.work_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.work_date IS 'Дата';


--
-- Name: COLUMN production_calendar.day_type_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.day_type_id IS 'Ссылка на справочник типов дней day_types';


--
-- Name: COLUMN production_calendar.description; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_calendar.description IS 'Описание праздника или причины переноса';


--
-- Name: project_buildings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_buildings (
    project_id integer NOT NULL,
    building_id integer NOT NULL
);


--
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
-- Name: TABLE project_composition; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_composition IS 'Состав проекта: задание ГИПа — какой отдел, какую марку/раздел, на какой стадии';


--
-- Name: COLUMN project_composition.stage_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_composition.stage_id IS 'Стадия, определённая ГИПом. Совместно с marka_type_id задаёт правила для документов';


--
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
-- Name: project_composition_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_composition_id_seq OWNED BY public.project_composition.id;


--
-- Name: project_mark_sets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_mark_sets (
    id integer NOT NULL,
    composition_id integer NOT NULL,
    marka_full text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    last_update timestamp with time zone,
    recipient_composition_id integer,
    released_on date,
    CONSTRAINT project_mark_sets_recipient_not_self CHECK (((recipient_composition_id IS NULL) OR (recipient_composition_id <> composition_id)))
);


--
-- Name: TABLE project_mark_sets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.project_mark_sets IS 'Реестр физических документов (комплектов РД и разделов ПД), созданных начальником бюро в рамках состава проекта';


--
-- Name: COLUMN project_mark_sets.composition_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.composition_id IS 'Ссылка на строку состава проекта (задание ГИПа). Несколько документов могут ссылаться на один composition_id';


--
-- Name: COLUMN project_mark_sets.marka_full; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.marka_full IS 'Полный уникальный шифр документа, вводится вручную (например, 053-22-14а-ТХ1, 053-22-14а-ОПЗ#1)';


--
-- Name: COLUMN project_mark_sets.recipient_composition_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.recipient_composition_id IS 'Строка состава получателя задания. NULL — обычный комплект; заполнено — задание смежному отделу. Признак задания выводится из этой колонки, отдельного флага нет.';


--
-- Name: COLUMN project_mark_sets.released_on; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.project_mark_sets.released_on IS 'Дата выпуска комплекта; NULL — не выпущен. Ставит начальник бюро вручную';


--
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
-- Name: project_mark_sets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_mark_sets_id_seq OWNED BY public.project_mark_sets.id;


--
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
-- Name: project_stages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_stages (
    id integer NOT NULL,
    stage_name character varying(50) NOT NULL
);


--
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
-- Name: project_stages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_stages_id_seq OWNED BY public.project_stages.id;


--
-- Name: project_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_status (
    id integer NOT NULL,
    status_name character varying(50) NOT NULL,
    description text
);


--
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
-- Name: project_status_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_status_id_seq OWNED BY public.project_status.id;


--
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
-- Name: project_status_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.project_status_log_id_seq OWNED BY public.project_status_log.id;


--
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
-- Name: project_status_log_view; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.project_status_log_view AS
 SELECT l.id AS log_id,
    p.code AS project_code,
    p.title AS project_title,
    ps_old.status_name AS old_status_name,
    ps_new.status_name AS new_status_name,
    l.comment,
    (COALESCE(e.full_name, u.username))::character varying(100) AS changed_by_name,
    l.changed_at
   FROM (((((public.project_status_log l
     JOIN public.projects p ON ((l.project_id = p.id)))
     LEFT JOIN public.project_status ps_old ON ((l.old_status_id = ps_old.id)))
     LEFT JOIN public.project_status ps_new ON ((l.new_status_id = ps_new.id)))
     LEFT JOIN public.users u ON ((l.changed_by = u.id)))
     LEFT JOIN public.employees e ON ((e.user_id = u.id)))
  ORDER BY l.changed_at DESC;


--
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
-- Name: projects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;


--
-- Name: role_permissions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_permissions (
    role_id integer NOT NULL,
    permission_id integer NOT NULL
);


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    description text
);


--
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
-- Name: roles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.roles_id_seq OWNED BY public.roles.id;


--
-- Name: test; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.test (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    city character varying(100)
);


--
-- Name: test_child; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.test_child (
    id integer NOT NULL,
    test_id integer NOT NULL,
    description character varying(255)
);


--
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
-- Name: test_child_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.test_child_id_seq OWNED BY public.test_child.id;


--
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
-- Name: test_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.test_id_seq OWNED BY public.test.id;


--
-- Name: user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_roles (
    user_id integer NOT NULL,
    role_id integer NOT NULL
);


--
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
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
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
-- Name: VIEW v_project_composition; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_project_composition IS 'Состав проекта с иерархией отделов и счётчиком физических документов';


--
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
-- Name: VIEW v_timesheet; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.v_timesheet IS 'Табель на презумпции явки: рабочий день = отработан по норме, если не накрыт периодом неявки. Часы считаются от employee_norms, от fact_calendar_cells не зависит';


--
-- Name: work_packages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.work_packages (
    id integer NOT NULL,
    mark_set_id integer NOT NULL,
    name text NOT NULL,
    description text,
    executor_id integer,
    status_id integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT work_packages_name_not_blank CHECK ((btrim(name) <> ''::text))
);


--
-- Name: TABLE work_packages; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.work_packages IS 'Пакет работ: часть комплекта по смыслу работы. Создаёт и назначает начальник бюро';


--
-- Name: work_packages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.work_packages ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.work_packages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: workshops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workshops (
    id integer NOT NULL,
    enterprise_id integer NOT NULL,
    workshop_name character varying(100) NOT NULL
);


--
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
-- Name: workshops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workshops_id_seq OWNED BY public.workshops.id;


--
-- Name: absence_periods id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods ALTER COLUMN id SET DEFAULT nextval('public.absence_periods_id_seq'::regclass);


--
-- Name: absence_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types ALTER COLUMN id SET DEFAULT nextval('public.absence_types_id_seq'::regclass);


--
-- Name: buildings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings ALTER COLUMN id SET DEFAULT nextval('public.buildings_id_seq'::regclass);


--
-- Name: day_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types ALTER COLUMN id SET DEFAULT nextval('public.day_types_id_seq'::regclass);


--
-- Name: departments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments ALTER COLUMN id SET DEFAULT nextval('public.departments_id_seq'::regclass);


--
-- Name: designer id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer ALTER COLUMN id SET DEFAULT nextval('public.designer_id_seq'::regclass);


--
-- Name: employee_assignments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments ALTER COLUMN id SET DEFAULT nextval('public.employee_assignments_id_seq'::regclass);


--
-- Name: employee_norms id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms ALTER COLUMN id SET DEFAULT nextval('public.employee_norms_id_seq'::regclass);


--
-- Name: employees id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees ALTER COLUMN id SET DEFAULT nextval('public.employees_id_seq'::regclass);


--
-- Name: enterprises id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises ALTER COLUMN id SET DEFAULT nextval('public.enterprises_id_seq'::regclass);


--
-- Name: fact_calendar_cells id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells ALTER COLUMN id SET DEFAULT nextval('public.fact_calendar_cells_id_seq'::regclass);


--
-- Name: gips id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips ALTER COLUMN id SET DEFAULT nextval('public.gips_id_seq'::regclass);


--
-- Name: kanban_tasks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks ALTER COLUMN id SET DEFAULT nextval('public.kanban_tasks_id_seq'::regclass);


--
-- Name: marka_types id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_types ALTER COLUMN id SET DEFAULT nextval('public.marka_types_id_seq'::regclass);


--
-- Name: norm_reasons id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons ALTER COLUMN id SET DEFAULT nextval('public.norm_reasons_id_seq'::regclass);


--
-- Name: permissions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);


--
-- Name: plan_calendar_cells id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells ALTER COLUMN id SET DEFAULT nextval('public.plan_calendar_cells_id_seq'::regclass);


--
-- Name: positions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions ALTER COLUMN id SET DEFAULT nextval('public.positions_id_seq'::regclass);


--
-- Name: project_composition id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition ALTER COLUMN id SET DEFAULT nextval('public.project_composition_id_seq'::regclass);


--
-- Name: project_mark_sets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets ALTER COLUMN id SET DEFAULT nextval('public.project_mark_sets_id_seq'::regclass);


--
-- Name: project_stages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages ALTER COLUMN id SET DEFAULT nextval('public.project_stages_id_seq'::regclass);


--
-- Name: project_status id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status ALTER COLUMN id SET DEFAULT nextval('public.project_status_id_seq'::regclass);


--
-- Name: project_status_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log ALTER COLUMN id SET DEFAULT nextval('public.project_status_log_id_seq'::regclass);


--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);


--
-- Name: roles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles ALTER COLUMN id SET DEFAULT nextval('public.roles_id_seq'::regclass);


--
-- Name: test id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test ALTER COLUMN id SET DEFAULT nextval('public.test_id_seq'::regclass);


--
-- Name: test_child id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child ALTER COLUMN id SET DEFAULT nextval('public.test_child_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: workshops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops ALTER COLUMN id SET DEFAULT nextval('public.workshops_id_seq'::regclass);


--
-- Name: absence_periods absence_periods_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(date_from, date_to, '[]'::text) WITH &&);


--
-- Name: absence_periods absence_periods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_pkey PRIMARY KEY (id);


--
-- Name: absence_types absence_types_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_code_key UNIQUE (code);


--
-- Name: absence_types absence_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_pkey PRIMARY KEY (id);


--
-- Name: absence_types absence_types_t13_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_t13_code_key UNIQUE (t13_code);


--
-- Name: absence_types absence_types_tab_letter_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_types
    ADD CONSTRAINT absence_types_tab_letter_key UNIQUE (tab_letter);


--
-- Name: admin_change_items admin_change_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_pkey PRIMARY KEY (id);


--
-- Name: admin_change_items admin_change_items_unique_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_unique_cell UNIQUE (changeset_id, layer, work_date);


--
-- Name: admin_changesets admin_changesets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_pkey PRIMARY KEY (id);


--
-- Name: building_workshops building_workshops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT building_workshops_pkey PRIMARY KEY (workshop_id, building_id);


--
-- Name: buildings buildings_id_enterprise_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_id_enterprise_id_key UNIQUE (id, enterprise_id);


--
-- Name: buildings buildings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_pkey PRIMARY KEY (id);


--
-- Name: day_types day_types_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types
    ADD CONSTRAINT day_types_code_key UNIQUE (code);


--
-- Name: day_types day_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_types
    ADD CONSTRAINT day_types_pkey PRIMARY KEY (id);


--
-- Name: department_markas department_markas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_pkey PRIMARY KEY (department_id, marka_type_id);


--
-- Name: departments departments_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_code_key UNIQUE (code);


--
-- Name: departments departments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_pkey PRIMARY KEY (id);


--
-- Name: designer designer_full_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer
    ADD CONSTRAINT designer_full_name_key UNIQUE (full_name);


--
-- Name: designer designer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.designer
    ADD CONSTRAINT designer_pkey PRIMARY KEY (id);


--
-- Name: employee_assignments employee_assignments_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(valid_from, valid_to, '[]'::text) WITH &&);


--
-- Name: employee_assignments employee_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_pkey PRIMARY KEY (id);


--
-- Name: employee_norms employee_norms_no_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_no_overlap EXCLUDE USING gist (employee_id WITH =, daterange(valid_from, valid_to, '[]'::text) WITH &&);


--
-- Name: employee_norms employee_norms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_pkey PRIMARY KEY (id);


--
-- Name: employees employees_personnel_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_personnel_number_key UNIQUE (personnel_number);


--
-- Name: employees employees_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_pkey PRIMARY KEY (id);


--
-- Name: employees employees_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_user_id_key UNIQUE (user_id);


--
-- Name: enterprises enterprises_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises
    ADD CONSTRAINT enterprises_name_key UNIQUE (name);


--
-- Name: enterprises enterprises_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enterprises
    ADD CONSTRAINT enterprises_pkey PRIMARY KEY (id);


--
-- Name: fact_calendar_cells fact_calendar_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_pkey PRIMARY KEY (id);


--
-- Name: fact_change_log fact_change_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_pkey PRIMARY KEY (id);


--
-- Name: fact_kanban_cells fact_kanban_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_pkey PRIMARY KEY (fact_cell_id);


--
-- Name: gips gips_full_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips
    ADD CONSTRAINT gips_full_name_key UNIQUE (full_name);


--
-- Name: gips gips_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gips
    ADD CONSTRAINT gips_pkey PRIMARY KEY (id);


--
-- Name: kanban_tasks kanban_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks
    ADD CONSTRAINT kanban_tasks_pkey PRIMARY KEY (id);


--
-- Name: marka_to_stage marka_to_stage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_pkey PRIMARY KEY (marka_type_id, stage_id);


--
-- Name: marka_types marka_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_types
    ADD CONSTRAINT marka_types_pkey PRIMARY KEY (id);


--
-- Name: norm_reasons norm_reasons_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons
    ADD CONSTRAINT norm_reasons_code_key UNIQUE (code);


--
-- Name: norm_reasons norm_reasons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.norm_reasons
    ADD CONSTRAINT norm_reasons_pkey PRIMARY KEY (id);


--
-- Name: package_statuses package_statuses_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_statuses
    ADD CONSTRAINT package_statuses_name_key UNIQUE (name);


--
-- Name: package_statuses package_statuses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_statuses
    ADD CONSTRAINT package_statuses_pkey PRIMARY KEY (id);


--
-- Name: permissions permissions_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_name_key UNIQUE (name);


--
-- Name: permissions permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);


--
-- Name: plan_calendar_cells plan_calendar_cells_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_pkey PRIMARY KEY (id);


--
-- Name: positions positions_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_name_key UNIQUE (name);


--
-- Name: positions positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.positions
    ADD CONSTRAINT positions_pkey PRIMARY KEY (id);


--
-- Name: production_calendar production_calendar_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_calendar
    ADD CONSTRAINT production_calendar_pkey PRIMARY KEY (work_date);


--
-- Name: project_buildings project_buildings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT project_buildings_pkey PRIMARY KEY (project_id, building_id);


--
-- Name: project_composition project_composition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_pkey PRIMARY KEY (id);


--
-- Name: project_mark_sets project_mark_sets_marka_full_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_marka_full_key UNIQUE (marka_full);


--
-- Name: project_mark_sets project_mark_sets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_pkey PRIMARY KEY (id);


--
-- Name: project_register project_register_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_pkey PRIMARY KEY (project_id);


--
-- Name: project_stages project_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages
    ADD CONSTRAINT project_stages_pkey PRIMARY KEY (id);


--
-- Name: project_stages project_stages_stage_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_stages
    ADD CONSTRAINT project_stages_stage_name_key UNIQUE (stage_name);


--
-- Name: project_status_log project_status_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_pkey PRIMARY KEY (id);


--
-- Name: project_status project_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status
    ADD CONSTRAINT project_status_pkey PRIMARY KEY (id);


--
-- Name: project_status project_status_status_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status
    ADD CONSTRAINT project_status_status_name_key UNIQUE (status_name);


--
-- Name: projects projects_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_code_key UNIQUE (code);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: projects projects_unique_pair; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_unique_pair UNIQUE (id, enterprise_id);


--
-- Name: role_permissions role_permissions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: roles roles_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_name_key UNIQUE (name);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: test_child test_child_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child
    ADD CONSTRAINT test_child_pkey PRIMARY KEY (id);


--
-- Name: test test_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test
    ADD CONSTRAINT test_name_key UNIQUE (name);


--
-- Name: test test_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test
    ADD CONSTRAINT test_pkey PRIMARY KEY (id);


--
-- Name: project_composition unique_composition; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT unique_composition UNIQUE (project_id, department_id, marka_type_id, stage_id);


--
-- Name: fact_calendar_cells unique_fact_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT unique_fact_cell UNIQUE (employee_id, work_date);


--
-- Name: plan_calendar_cells unique_plan_cell; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT unique_plan_cell UNIQUE (employee_id, work_date);


--
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (user_id, role_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: work_packages work_packages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_packages
    ADD CONSTRAINT work_packages_pkey PRIMARY KEY (id);


--
-- Name: workshops workshops_enterprise_id_workshop_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_enterprise_id_workshop_name_key UNIQUE (enterprise_id, workshop_name);


--
-- Name: workshops workshops_id_enterprise_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_id_enterprise_id_key UNIQUE (id, enterprise_id);


--
-- Name: workshops workshops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_pkey PRIMARY KEY (id);


--
-- Name: idx_absence_periods_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_absence_periods_employee ON public.absence_periods USING btree (employee_id, date_from);


--
-- Name: idx_admin_changesets_employee_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_changesets_employee_created ON public.admin_changesets USING btree (employee_id, created_at DESC);


--
-- Name: idx_assignments_dept; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assignments_dept ON public.employee_assignments USING btree (department_id);


--
-- Name: idx_assignments_employee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assignments_employee ON public.employee_assignments USING btree (employee_id, valid_from);


--
-- Name: idx_composition_dept_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_composition_dept_stage ON public.project_composition USING btree (department_id, stage_id);


--
-- Name: idx_departments_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_departments_parent ON public.departments USING btree (parent_id);


--
-- Name: idx_employee_norms_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_employee_norms_lookup ON public.employee_norms USING btree (employee_id, valid_from);


--
-- Name: idx_fact_cells_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_cells_employee_date ON public.fact_calendar_cells USING btree (employee_id, work_date);


--
-- Name: idx_fact_cells_package; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_cells_package ON public.fact_calendar_cells USING btree (package_id);


--
-- Name: idx_fact_change_log_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_change_log_employee_date ON public.fact_change_log USING btree (employee_id, work_date);


--
-- Name: idx_fact_kanban_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fact_kanban_task ON public.fact_kanban_cells USING btree (kanban_task_id);


--
-- Name: idx_kanban_mark_set; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kanban_mark_set ON public.kanban_tasks USING btree (mark_set_id);


--
-- Name: idx_marka_types_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_marka_types_code ON public.marka_types USING btree (code);


--
-- Name: idx_plan_cells_employee_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_cells_employee_date ON public.plan_calendar_cells USING btree (employee_id, work_date);


--
-- Name: idx_plan_cells_package; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_cells_package ON public.plan_calendar_cells USING btree (package_id);


--
-- Name: idx_sets_composition; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_composition ON public.project_mark_sets USING btree (composition_id);


--
-- Name: idx_sets_recipient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sets_recipient ON public.project_mark_sets USING btree (recipient_composition_id) WHERE (recipient_composition_id IS NOT NULL);


--
-- Name: idx_status_log_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_status_log_project ON public.project_status_log USING btree (project_id);


--
-- Name: idx_work_packages_executor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_work_packages_executor ON public.work_packages USING btree (executor_id);


--
-- Name: uq_work_packages_set_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_work_packages_set_name ON public.work_packages USING btree (mark_set_id, lower(btrim(name)));


--
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
-- Name: absence_periods trg_absence_displaces_fact; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_absence_displaces_fact AFTER INSERT OR UPDATE OF employee_id, date_from, date_to ON public.absence_periods FOR EACH ROW EXECUTE FUNCTION public.trg_absence_displaces_fact();


--
-- Name: project_composition trg_composition_validity; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_composition_validity BEFORE INSERT OR UPDATE ON public.project_composition FOR EACH ROW EXECUTE FUNCTION public.trg_check_composition_validity();


--
-- Name: fact_calendar_cells trg_fact_check_absence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_fact_check_absence BEFORE INSERT OR UPDATE ON public.fact_calendar_cells FOR EACH ROW EXECUTE FUNCTION public.trg_fact_check_absence();


--
-- Name: projects update_projects_timestamp; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_projects_timestamp BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.update_timestamp();


--
-- Name: absence_periods absence_periods_absence_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_absence_type_id_fkey FOREIGN KEY (absence_type_id) REFERENCES public.absence_types(id) ON DELETE RESTRICT;


--
-- Name: absence_periods absence_periods_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: absence_periods absence_periods_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.absence_periods
    ADD CONSTRAINT absence_periods_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- Name: admin_change_items admin_change_items_changeset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_changeset_id_fkey FOREIGN KEY (changeset_id) REFERENCES public.admin_changesets(id) ON DELETE CASCADE;


--
-- Name: admin_change_items admin_change_items_new_package_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_new_package_id_fkey FOREIGN KEY (new_package_id) REFERENCES public.work_packages(id) ON DELETE SET NULL;


--
-- Name: admin_change_items admin_change_items_old_package_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_change_items
    ADD CONSTRAINT admin_change_items_old_package_id_fkey FOREIGN KEY (old_package_id) REFERENCES public.work_packages(id) ON DELETE SET NULL;


--
-- Name: admin_changesets admin_changesets_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: admin_changesets admin_changesets_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_changesets
    ADD CONSTRAINT admin_changesets_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- Name: buildings buildings_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.buildings
    ADD CONSTRAINT buildings_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: department_markas department_markas_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE CASCADE;


--
-- Name: department_markas department_markas_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.department_markas
    ADD CONSTRAINT department_markas_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE CASCADE;


--
-- Name: departments departments_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.departments
    ADD CONSTRAINT departments_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.departments(id) ON DELETE SET NULL;


--
-- Name: employee_assignments employee_assignments_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE RESTRICT;


--
-- Name: employee_assignments employee_assignments_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE CASCADE;


--
-- Name: employee_assignments employee_assignments_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_assignments
    ADD CONSTRAINT employee_assignments_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.positions(id) ON DELETE RESTRICT;


--
-- Name: employee_norms employee_norms_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: employee_norms employee_norms_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE CASCADE;


--
-- Name: employee_norms employee_norms_reason_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employee_norms
    ADD CONSTRAINT employee_norms_reason_id_fkey FOREIGN KEY (reason_id) REFERENCES public.norm_reasons(id) ON DELETE RESTRICT;


--
-- Name: employees employees_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.employees
    ADD CONSTRAINT employees_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: fact_calendar_cells fact_calendar_cells_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- Name: fact_calendar_cells fact_calendar_cells_package_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_package_id_fkey FOREIGN KEY (package_id) REFERENCES public.work_packages(id) ON DELETE CASCADE;


--
-- Name: fact_calendar_cells fact_calendar_cells_work_date_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_calendar_cells
    ADD CONSTRAINT fact_calendar_cells_work_date_fkey FOREIGN KEY (work_date) REFERENCES public.production_calendar(work_date);


--
-- Name: fact_change_log fact_change_log_absence_period_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_absence_period_id_fkey FOREIGN KEY (absence_period_id) REFERENCES public.absence_periods(id) ON DELETE SET NULL;


--
-- Name: fact_change_log fact_change_log_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: fact_change_log fact_change_log_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- Name: fact_change_log fact_change_log_package_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_change_log
    ADD CONSTRAINT fact_change_log_package_id_fkey FOREIGN KEY (package_id) REFERENCES public.work_packages(id) ON DELETE SET NULL;


--
-- Name: fact_kanban_cells fact_kanban_cells_fact_cell_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_fact_cell_id_fkey FOREIGN KEY (fact_cell_id) REFERENCES public.fact_calendar_cells(id) ON DELETE CASCADE;


--
-- Name: fact_kanban_cells fact_kanban_cells_kanban_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_kanban_cells
    ADD CONSTRAINT fact_kanban_cells_kanban_task_id_fkey FOREIGN KEY (kanban_task_id) REFERENCES public.kanban_tasks(id) ON DELETE CASCADE;


--
-- Name: building_workshops fk_bw_building; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT fk_bw_building FOREIGN KEY (building_id, enterprise_id) REFERENCES public.buildings(id, enterprise_id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: building_workshops fk_bw_workshop; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.building_workshops
    ADD CONSTRAINT fk_bw_workshop FOREIGN KEY (workshop_id, enterprise_id) REFERENCES public.workshops(id, enterprise_id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: project_buildings fk_project_buildings_building; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT fk_project_buildings_building FOREIGN KEY (building_id) REFERENCES public.buildings(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: project_buildings fk_project_buildings_project; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_buildings
    ADD CONSTRAINT fk_project_buildings_project FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: role_permissions fk_rp_permission; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT fk_rp_permission FOREIGN KEY (permission_id) REFERENCES public.permissions(id) ON DELETE CASCADE;


--
-- Name: role_permissions fk_rp_role; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_permissions
    ADD CONSTRAINT fk_rp_role FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- Name: user_roles fk_ur_role; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_ur_role FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- Name: user_roles fk_ur_user; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_ur_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: kanban_tasks kanban_tasks_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.kanban_tasks
    ADD CONSTRAINT kanban_tasks_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE CASCADE;


--
-- Name: marka_to_stage marka_to_stage_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE CASCADE;


--
-- Name: marka_to_stage marka_to_stage_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.marka_to_stage
    ADD CONSTRAINT marka_to_stage_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON DELETE CASCADE;


--
-- Name: plan_calendar_cells plan_calendar_cells_employee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES public.employees(id) ON DELETE RESTRICT;


--
-- Name: plan_calendar_cells plan_calendar_cells_package_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_package_id_fkey FOREIGN KEY (package_id) REFERENCES public.work_packages(id) ON DELETE CASCADE;


--
-- Name: plan_calendar_cells plan_calendar_cells_work_date_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_calendar_cells
    ADD CONSTRAINT plan_calendar_cells_work_date_fkey FOREIGN KEY (work_date) REFERENCES public.production_calendar(work_date);


--
-- Name: production_calendar production_calendar_day_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_calendar
    ADD CONSTRAINT production_calendar_day_type_id_fkey FOREIGN KEY (day_type_id) REFERENCES public.day_types(id) ON DELETE RESTRICT;


--
-- Name: project_composition project_composition_department_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_department_id_fkey FOREIGN KEY (department_id) REFERENCES public.departments(id) ON DELETE RESTRICT;


--
-- Name: project_composition project_composition_marka_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_marka_type_id_fkey FOREIGN KEY (marka_type_id) REFERENCES public.marka_types(id) ON DELETE RESTRICT;


--
-- Name: project_composition project_composition_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_composition project_composition_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_composition
    ADD CONSTRAINT project_composition_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON DELETE RESTRICT;


--
-- Name: project_mark_sets project_mark_sets_composition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_composition_id_fkey FOREIGN KEY (composition_id) REFERENCES public.project_composition(id) ON DELETE RESTRICT;


--
-- Name: project_mark_sets project_mark_sets_recipient_composition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_mark_sets
    ADD CONSTRAINT project_mark_sets_recipient_composition_id_fkey FOREIGN KEY (recipient_composition_id) REFERENCES public.project_composition(id) ON DELETE RESTRICT;


--
-- Name: project_register project_register_gip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_gip_id_fkey FOREIGN KEY (gip_id) REFERENCES public.gips(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: project_register project_register_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_register
    ADD CONSTRAINT project_register_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: project_status_log project_status_log_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.users(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: project_status_log project_status_log_new_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_new_status_id_fkey FOREIGN KEY (new_status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE RESTRICT;


--
-- Name: project_status_log project_status_log_old_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_old_status_id_fkey FOREIGN KEY (old_status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: project_status_log project_status_log_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_status_log
    ADD CONSTRAINT project_status_log_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: projects projects_designer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_designer_id_fkey FOREIGN KEY (designer_id) REFERENCES public.designer(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: projects projects_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: projects projects_stage_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_stage_id_fkey FOREIGN KEY (stage_id) REFERENCES public.project_stages(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: projects projects_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_status_id_fkey FOREIGN KEY (status_id) REFERENCES public.project_status(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: projects projects_workshop_enterprise_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_workshop_enterprise_fkey FOREIGN KEY (workshop_id, enterprise_id) REFERENCES public.workshops(id, enterprise_id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: test_child test_child_test_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.test_child
    ADD CONSTRAINT test_child_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.test(id) ON DELETE CASCADE;


--
-- Name: work_packages work_packages_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_packages
    ADD CONSTRAINT work_packages_executor_id_fkey FOREIGN KEY (executor_id) REFERENCES public.employees(id) ON DELETE SET NULL;


--
-- Name: work_packages work_packages_mark_set_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_packages
    ADD CONSTRAINT work_packages_mark_set_id_fkey FOREIGN KEY (mark_set_id) REFERENCES public.project_mark_sets(id) ON DELETE CASCADE;


--
-- Name: work_packages work_packages_status_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.work_packages
    ADD CONSTRAINT work_packages_status_id_fkey FOREIGN KEY (status_id) REFERENCES public.package_statuses(id) ON DELETE RESTRICT;


--
-- Name: workshops workshops_enterprise_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workshops
    ADD CONSTRAINT workshops_enterprise_id_fkey FOREIGN KEY (enterprise_id) REFERENCES public.enterprises(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 8fgSqLGUwbGE10k00bCscatAsEGiDWFjyqzKkAVFJORhlAG2ScdBv3vH7G9ZVRb

