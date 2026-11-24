# זכותי: סוכן לבדיקת זכויות והטבות (אבן דרך 1: כלים ומצב)

פרויקט בקורס סוכני בינה מלאכותית. **כל הנתונים סינתטיים** (`data/rules.json`): ההטבות, הספים והסכומים בדויים.
הפלט הוא הערכה ראשונית לצורכי הדגמה בלבד.

מגישים: [רזניק ניקיטה]  |  commit מוגש: `[להדביק כאן את מזהה ה-commit]`

## הרצה ב-Google Colab (חובה)

1. פתחו את `Milestone1_Agent_Colab.ipynb` ב-Colab: [Open in Colab](https://colab.research.google.com/github/<USER>/<REPO>/blob/main/Milestone1_Agent_Colab.ipynb)  (להחליף את `https://github.com/Nikvai/zchuti-milestone1`)
2. בתא הראשון עדכנו את `REPO_URL` והריצו את כל התאים מהתחלה, בקולאב נקי.
3. המחברת מבצעת `git clone`, `pip install -r requirements.txt`, מריצה את הבדיקות ואת ההדגמה.

כברירת מחדל רץ **רכיב החלטה מדומה, בלי מפתח ובלי עלות**. רכיב עם מודל אמיתי נטען רק אם מוגדר `ANTHROPIC_API_KEY`
(ב-Colab: Secrets). המפתח לעולם לא נכתב לקוד או למאגר.

## הרצה מקומית

```
pip install -r requirements.txt
python -m pytest -q                 # כל הבדיקות, פקודה אחת, בלי מפתח ובלי שירות חיצוני
python -m src.evaluate              # 8 מקרי הבדיקה של ההצעה והמדד העיקרי
python -m src.compare_context       # השוואת הקשר מלא מול מצומצם
python -m src.make_traces           # יצירת קבצי העקבות ב-docs/traces/
```

## מבנה

```
src/tools.py            הכלים והחוזים שלהם (סכמות, אימות, הרשאות, ניסיונות חוזרים, timeout)
src/state.py            סכמת המצב, הפרדה בין שיחות, בניית הקשר (מלא / מצומצם)
src/agent.py            לולאת הסוכן, העקבות, רכיב ההחלטה המדומה ומתאם המודל האופציונלי
src/mocks.py            כלי מדומה שנכשל זמנית (מסומן במפורש כמדומה)
src/evaluate.py         מקרי הבדיקה של ההצעה והמדד
src/compare_context.py  ההשוואה ברכיב 4
tests/                  בדיקות (pytest)
data/rules.json         נתונים סינתטיים
docs/milestone1.md      מסמך התיעוד
docs/ai_usage.md        נספח שימוש ב-AI
docs/traces/            קבצי עקבות של הרצות אמיתיות
```

## הכלים (מתוך טבלת הכלים בהצעה)

| כלי | סוג | אישור |
|---|---|---|
| `check_policy_rules` | קריאה | לא |
| `verify_user_eligibility` | חישוב (כללים בקוד) | לא |
| `generate_rights_report` | חישוב (טקסט בלבד, לא שולח ולא שומר) | לא |

לכל כלי חוזה מפורש (שם, תיאור, סכמת קלט וסכמת פלט) בתוך `src/tools.py`.
