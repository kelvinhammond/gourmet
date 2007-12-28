# For testing - DELETE ME
import sys
sys.path.append('/usr/share/')
# End for testing - DELETE ME

import os.path
from gourmet.gdebug import debug, TimeAction
import re, pickle, string, os.path, string, time
from gettext import gettext as _
import gourmet.gglobals as gglobals
from gourmet import Undo, keymanager, convert
from gourmet.defaults import lang as defaults
import gourmet.nutrition.parser_data
import StringIO
from gourmet import ImageExtras
import gourmet.version
import gourmet.recipeIdentifier as recipeIdentifier

import sqlalchemy, sqlalchemy.orm
from sqlalchemy import Integer, Binary, String, Float, Boolean, Numeric, Table, Column, ForeignKey
from sqlalchemy.sql import and_, or_

def map_type_to_sqlalchemy (typ):
    """A convenience method -- take a string type and map it into a
    sqlalchemy type.
    """
    if typ=='int': return Integer()
    if typ.find('char(')==0:
        return String(
            length=int(typ[typ.find('(')+1:typ.find(')')])
            )
    if typ=='text': return String(length=None)
    if typ=='bool': return Boolean()
    if typ=='float': return Float()
    if typ=='binary': return Binary()

def fix_colnames (dict, *tables):
    """Map column names to sqlalchemy columns.
    """
    # This is a convenience method -- throughout Gourmet, the column
    # names are handed around as strings. This converts them into the
    # object sqlalchemy prefers.
    newdict =  {}
    for k,v in dict.items():
        got_prop = False
        for t in tables:
            try:
                newdict[getattr(t.c,k)]=v
            except:
                1
            else:
                got_prop = True
        if not got_prop: raise ValueError("Could not find column %s in tables %s"%(k,tables))
    return newdict

def make_simple_select_arg (criteria,*tables):
    args = []
    for k,v in fix_colnames(criteria,*tables).items():
        args.append(k==v)
    if len(args)>1:
        return [and_(*args)]
    elif args:
        return [args[0]]
    else:
        return []

def make_order_by (sort_by, table, count_by=None):
        ret = []
        for col,dir in sort_by:
            if col=='count' and not hasattr(table.c,'count'):
                col = sqlalchemy.func.count(getattr(table.c,count_by))
            else:
                col = getattr(table.c,col)
            if dir==1: # Ascending
                ret.append(sqlalchemy.asc(col))
            else:
                ret.append(sqlalchemy.desc(col))
        return ret
    
class DBObject:
    pass


# CHANGES SINCE PREVIOUS VERSIONS...
# categories_table: id -> recipe_id, category_entry_id -> id
# ingredients_table: ingredient_id -> id, id -> recipe_id

class RecData:

    """RecData is our base class for handling database connections.

    Subclasses implement specific backends, such as metakit, sqlite, etc."""

    # constants for determining how to get amounts when there are ranges.
    AMT_MODE_LOW = 0
    AMT_MODE_AVERAGE = 1
    AMT_MODE_HIGH = 2

    tables = [
        # becomes nutritionaliases_table
        ]


    def __init__ (self, file=os.path.join(gglobals.gourmetdir,'recipes.db'),
                  custom_url=None):
        # hooks run after adding, modifying or deleting a recipe.
        # Each hook is handed the recipe, except for delete_hooks,
        # which is handed the ID (since the recipe has been deleted)
        if custom_url:
            self.url = custom_url
            self.filename = None
        else:
            self.filename = file
            self.url = 'sqlite:///' + self.filename
        self.add_hooks = []
        self.modify_hooks = []
        self.delete_hooks = []
        self.add_ing_hooks = []
        timer = TimeAction('initialize_connection + setup_tables',2)
        self.initialize_connection()
        self.setup_tables()        
        timer.end()

    # Basic setup functions

    def initialize_connection (self):
        """Initialize our database connection.
        
        This should also set self.new_db accordingly"""
        if self.filename:
            self.new_db = not os.path.exists(self.filename)
            print 'Connecting to file ',self.filename
        else:
            self.new_db = True # ??? How will we do this now?
        self.db = sqlalchemy.create_engine(self.url,strategy='threadlocal') 
        self.db.begin()
        self.metadata = sqlalchemy.MetaData(self.db)
        self.session = sqlalchemy.create_session()
        #raise NotImplementedError
        def regexp(expr, item):
            if item:
                return re.search(expr,item,re.IGNORECASE) is not None
            else:
                return False
        def instr(s,subs): return s.lower().find(subs.lower())+1
        # Workaround to create REGEXP function in sqlite
        if self.url.startswith('sqlite'):
            sqlite_connection = self.db.connect().connection
            sqlite_connection.create_function('regexp',2,regexp)
            c = sqlite_connection.cursor()
            c.execute('select name from sqlite_master')
            #sqlite_connection.create_function('instr',2,instr)

    def save (self):
        """Save our database (if we have a separate 'save' concept)"""
        row = self.fetch_one(self.info_table)
        if row:
            self.do_modify(
                self.info_table,
                row,
                {'last_access':time.time()},
                id_col = None
                )
        else:
            self.do_add(
                self.info_table,
                {'last_access':time.time()}
                )
        self.db.commit()

    def __setup_object_for_table (self, table, klass):
        self.__table_to_object__[table] = klass
        #print 'Mapping ',repr(klass),'->',repr(table)
        if True in [col.primary_key for col in table.columns]:
            sqlalchemy.orm.mapper(klass,table)
        else:
            # if there's no primary key...
            sqlalchemy.orm.mapper(klass,table,primary_key='rowid')

    def setup_tables (self):
        """
        Subclasses should do any necessary adjustments/tweaking before calling
        this function."""
        # Info table - for versioning info
        self.__table_to_object__ = {}
        self.setup_base_tables()
        self.setup_shopper_tables() # could one day be part of a plugin
        self.setup_nutrition_tables() # could one day be part of a plugin
        self.metadata.create_all()
        self.update_version_info(gourmet.version.version)        

    def setup_base_tables (self):
        self.setup_info_table()
        self.setup_recipe_table()
        self.setup_category_table()
        self.setup_ingredient_table()        
        
    def setup_info_table (self):
        self.info_table = Table('info',self.metadata,
                                Column('version_super',Integer(),**{}), # three part version numbers 2.1.10, etc. 1.0.0
                                Column('version_major',Integer(),**{}),
                                Column('version_minor',Integer(),**{}),
                                Column('last_access',Integer(),**{})
                                 )
        class Info (object):
            pass
        self.__setup_object_for_table(self.info_table, Info)

    def setup_recipe_table (self):
        self.recipe_table = Table('recipe',self.metadata,
                                  Column('id',Integer(),**{'primary_key':True}),
                                  Column('title',String(length=None),**{}),
                                  Column('instructions',String(length=None),**{}),
                                  Column('modifications',String(length=None),**{}),
                                  Column('cuisine',String(length=None),**{}),
                                  Column('rating',Integer(),**{}),
                                  Column('description',String(length=None),**{}),
                                  Column('source',String(length=None),**{}),
                                  Column('preptime',Integer(),**{}),
                                  Column('cooktime',Integer(),**{}),
                                  Column('servings',Float(),**{}),
                                  Column('image',Binary(),**{}),
                                  Column('thumb',Binary(),**{}),
                                  Column('deleted',Boolean(),**{}),
                                  # A hash for uniquely identifying a recipe (based on title etc)
                                  Column('recipe_hash',String(length=32),**{}),
                                  # A hash for uniquely identifying a recipe (based on ingredients)
                                  Column('ingredient_hash',String(length=32),**{}),
                                  Column('link',String(length=None),**{}), # A field for a URL -- we ought to know about URLs
                                  Column('last_modified',Integer(),**{}),
                                  ) # RECIPE_TABLE_DESC
        class Recipe (object): pass
        self.__setup_object_for_table(self.recipe_table,Recipe)

    def setup_category_table (self):
        self.categories_table = Table('categories',self.metadata,
                                    Column('id',Integer(),primary_key=True),
                                    Column('recipe_id',Integer,ForeignKey('recipe.id'),**{}), #recipe ID
                                    Column('category',String(length=None),**{}) # Category ID
                                    ) # CATEGORY_TABLE_DESC
        class Category (object): pass
        self.__setup_object_for_table(self.categories_table,Category)

    def setup_ingredient_table (self):
        self.ingredients_table = Table('ingredients',self.metadata,
                                       Column('id',Integer(),primary_key=True),
                                       Column('recipe_id',Integer,ForeignKey('recipe.id'),**{}),
                                       Column('refid',Integer,ForeignKey('recipe.id'),**{}),
                                       Column('unit',String(length=None),**{}),
                                       Column('amount',Float(),**{}),
                                       Column('rangeamount',Float(),**{}),
                                       Column('item',String(length=None),**{}),
                                       Column('ingkey',String(length=None),**{}),
                                       Column('optional',Boolean(),**{}),
                                       #Integer so we can distinguish unset from False
                                       Column('shopoptional',Integer(),**{}), 
                                       Column('inggroup',String(length=None),**{}),
                                       Column('position',Integer(),**{}),
                                       Column('deleted',Boolean(),**{}),
                                       )
        class Ingredient (object): pass
        self.__setup_object_for_table(self.ingredients_table, Ingredient)

    def setup_keylookup_table (self):
        # Keylookup table - for speedy keylookup
        self.keylookup_table = Table('keylookup',self.metadata,
                                     Column('id',Integer(),primary_key=True),
                                     Column('word',String(length=None),**{}),
                                      Column('item',String(length=None),**{}),
                                      Column('ingkey',String(length=None),**{}),
                                      Column('count',Integer(),**{})
                                     ) # INGKEY_LOOKUP_TABLE_DESC
        class KeyLookup (object): pass
        self.__setup_object_for_table(self.keylookup_table, KeyLookup)

    def setup_shopper_tables (self):
        
        self.setup_keylookup_table()

        # shopcats - Keep track of which shoppin category ingredients are in...
        self.shopcats_table = Table('shopcats',self.metadata,
                                    Column('ingkey',String(length=None),**{'primary_key':True}),
                                    Column('shopcategory',String(length=None),**{}),
                                    Column('position',Integer(),**{}),
                                    )
        class ShopCat (object): pass
        self.__setup_object_for_table(self.shopcats_table, ShopCat)
        
        # shopcatsorder - Keep track of the order of shopping categories
        self.shopcatsorder_table = Table('shopcatsorder',self.metadata,
                                         Column('shopcategory',String(length=None),**{'primary_key':True}),
                                         Column('position',Integer(),**{}),
                                         )
        class ShopCatOrder (object): pass
        self.__setup_object_for_table(self.shopcatsorder_table, ShopCatOrder)
        
        # pantry table -- which items are in the "pantry" (i.e. not to
        # be added to the shopping list)
        self.pantry_table = Table('pantry',self.metadata,
                                  Column('ingkey',String(length=None),**{'primary_key':True}),
                                  Column('pantry',Boolean(),**{}),
                                  )
        class Pantry (object): pass
        self.__setup_object_for_table(self.pantry_table, Pantry)

        # Keep track of the density of items...
        self.density_table = Table('density',self.metadata,
                                   Column('dkey',String(length=150),**{'primary_key':True}),
                                   Column('value',String(length=150),**{})
                                   )
        class Density (object): pass
        self.__setup_object_for_table(self.density_table, Density)
        
        self.crossunitdict_table = Table('crossunitdict',self.metadata,                                         
                                         Column('cukey',String(length=150),**{'primary_key':True}),
                                         Column('value',String(length=150),**{}),
                                         )
        class CrossUnit (object): pass
        self.__setup_object_for_table(self.crossunitdict_table,CrossUnit)
        
        self.unitdict_table = Table('unitdict',self.metadata,
                                    Column('ukey',String(length=150),**{'primary_key':True}),
                                    Column('value',String(length=150),**{}),
                                    )
        class Unitdict (object):
            pass
        self.__setup_object_for_table(self.unitdict_table, Unitdict)
        
        self.convtable_table = Table('convtable',self.metadata,
                                     Column('ckey',String(length=150),**{'primary_key':True}),
                                     Column('value',String(length=150),**{})
                                     )
        class Convtable (object):
            pass
        self.__setup_object_for_table(self.convtable_table, Convtable)

    def setup_usda_weights_table (self):
        self.usda_weights_table = Table('usda_weights',self.metadata,
                                        Column('id',Integer(),primary_key=True),
                                        *[Column(name,map_type_to_sqlalchemy(typ),**{})
                                          for lname,name,typ in gourmet.nutrition.parser_data.WEIGHT_FIELDS]
                                        )
        class UsdaWeight (object):
            pass
        self.__setup_object_for_table(self.usda_weights_table, UsdaWeight)

    def setup_nutrition_tables (self):

        cols = [Column(name,map_type_to_sqlalchemy(typ),**(name=='ndbno' and {'primary_key':True} or {}))
                 for lname,name,typ in gourmet.nutrition.parser_data.NUTRITION_FIELDS
                 ] + [Column('foodgroup',String(length=None),**{})]
        #print 'nutrition cols:',cols
        self.nutrition_table = Table('nutrition',self.metadata,
                                     *cols
                                     )
        class Nutrition (object):
            pass
        self.__setup_object_for_table(self.nutrition_table, Nutrition)
        
        self.setup_usda_weights_table()

        self.nutritionaliases_table = Table('nutritionaliases',self.metadata,                                            
                                            Column('ingkey',String(length=None),**{'primary_key':True}),
                                            Column('ndbno',Integer,ForeignKey('nutrition.ndbno'),**{}),
                                            Column('density_equivalent',String(length=20),**{}),)
        class NutritionAlias (object): pass
        self.__setup_object_for_table(self.nutritionaliases_table, NutritionAlias)

        self.nutritionconversions_table = Table('nutritionconversions',self.metadata,
                                                Column('id',Integer(),primary_key=True),
                                                Column('ingkey',String(length=None),**{}),
                                                Column('unit',String(length=None),**{}), 
                                                Column('factor',Float(),**{}), # Factor is the amount we multiply
                                                # from unit to get 100 grams
                                                ) # NUTRITION_CONVERSIONS
        class NutritionConversion (object): pass
        self.__setup_object_for_table(self.nutritionconversions_table, NutritionConversion)
        
        for table in self.tables:
            name,columns = table
            setattr(self,name+'_table',
                    Table(name,
                          self.metadata,
                          *[Column(col[0],col[1],**col[2]) for col in columns],
                          **{'schema':None}
                          ))

    def update_version_info (self, version_string):
        """Report our version to the database.

        If necessary, we'll do some version-dependent updates to the GUI
        """
        stored_info = self.fetch_one(self.info_table)
        if not stored_info or not stored_info.version_major:
            # Default info -- the last version before we added the
            # version tracker...
            default_info = {'version_super':0,
                             'version_major':11,
                             'version_minor':0}
            if not stored_info:
                self.do_add(self.info_table,
                            default_info)
            else:
                self.do_modify(
                    self.info_table,
                    stored_info,
                    default_info)
            stored_info = self.fetch_one(self.info_table)            
        version = [s for s in version_string.split('.')]
        current_super = int(version[0])
        current_major = int(version[1])
        current_minor = int(version[2])
        ### Code for updates between versions...

        if not self.new_db:

            # Version < 0.11.4 -> version >= 0.11.4... fix up screwed up keylookup_table tables...
            # We don't actually do this yet... (FIXME)
            #print 'STORED_INFO:',stored_info.version_super,stored_info.version_major,stored_info.version_minor
            if stored_info.version_super == 0 and stored_info.version_major <= 11 and stored_info.version_minor <= 3:
                print 'Fixing broken ingredient-key view from earlier versions.'
                # Drop keylookup_table table, which wasn't being properly kept up
                # to date...
                self.delete_by_criteria(self.keylookup_table,{}) 
                # And update it in accord with current ingredients (less
                # than an ideal decision, alas)
                for ingredient in self.fetch_all(self.ingredients_table,deleted=False):
                    self.add_ing_to_keydic(ingredient.item,ingredient.ingkey)

            if stored_info.version_super == 0 and stored_info.version_major < 14:
                # Name changes to make working with IDs make more sense
                # (i.e. the column named 'id' should always be a unique
                # identifier for a given table -- it should not be used to
                # refer to the IDs from *other* tables
                print 'Upgrade from < 0.14'
                self.alter_table('categories',self.setup_category_table,
                                 {'id':'recipe_id'},['category'])
                print 'RECREATE INGREDIENTS TABLE (This could take a while...)'
                self.alter_table('ingredients',self.setup_ingredient_table,
                                 {'id':'recipe_id'},
                                 ['refid', 'unit', 'amount', 'rangeamount',
                                  'item', 'ingkey', 'optional', 'shopoptional',
                                  'inggroup', 'position', 'deleted'])
                print 'RECREATE KEYLOOKUP TABLE'
                self.alter_table('keylookup',self.setup_keylookup_table,
                                 {},['word','item','ingkey','count'])
                print 'RECREATE USDA WEIGHTS TABLE'
                self.alter_table('usda_weights',self.setup_usda_weights_table,{},
                                 [name for lname,name,typ in gourmet.nutrition.parser_data.WEIGHT_FIELDS])
            # Add recipe_hash, ingredient_hash and link fields
            # (These all get added in 0.13.0)
            if stored_info.version_super == 0 and stored_info.version_major <= 12:
                print 'UPDATE FROM < 0.13.0...'
                # Don't change the table defs here without changing them
                # above as well (for new users) - sorry for the stupid
                # repetition of code.
                self.add_column_to_table(self.recipe_table,('last_modified',Integer(),{}))
                self.add_column_to_table(self.recipe_table,('recipe_hash','String(length=32)',{}))
                self.add_column_to_table(self.recipe_table,('ingredient_hash','String(length=32)',{}))
                # Add a link field...
                self.add_column_to_table(self.recipe_table,('link',String(length=None),{}))
                print 'Searching for links in old recipe fields...'
                URL_SOURCES = ['instructions','source','modifications']
                recs = self.search_recipes(
                    [
                    {'column':col,
                     'operator':'LIKE',
                     'search':'%://%',
                     'logic':'OR'
                     }
                    for col in URL_SOURCES
                    ])
                for r in recs:
                    rec_url = ''
                    for src in URL_SOURCES:
                        blob = getattr(r,src)
                        url = None
                        if blob:
                            m = re.search('\w+://[^ ]*',blob)
                            if m:
                                rec_url = blob[m.start():m.end()]
                                if rec_url[-1] in ['.',')',',',';',':']:
                                    # Strip off trailing punctuation on
                                    # the assumption this is part of a
                                    # sentence -- this will break some
                                    # URLs, but hopefully rarely enough it
                                    # won't harm (m)any users.
                                    rec_url = rec_url[:-1]
                                break
                    if rec_url:
                        if r.source==rec_url:
                            new_source = rec_url.split('://')[1]
                            new_source = new_source.split('/')[0]
                            self.do_modify_rec(
                                r,
                                {'link':rec_url,
                                 'source':new_source,
                                 }
                                )
                        else:
                            self.do_modify_rec(
                                r,
                                {'link':rec_url,}
                                )
                # Add hash values to identify all recipes...
                for r in self.fetch_all(self.recipe_table): self.update_hashes(r)
                        
        ### End of code for updates between versions...
        if (current_super!=stored_info.version_super
            or
            current_major!=stored_info.version_major
            or
            current_minor!=stored_info.version_minor
            ):
            self.do_modify(
                self.info_table,
                stored_info,
                {'version_super':current_super,
                 'version_major':current_major,
                 'version_minor':current_minor,},
                id_col=None
                )

    def run_hooks (self, hooks, *args):
        """A basic hook-running function. We use hooks to allow parts of the application
        to tag onto data-modifying events and e.g. update the display"""
        for h in hooks:
            t = TimeAction('running hook %s with args %s'%(h,args),3)
            h(*args)
            t.end()

    # basic DB access functions
    def fetch_all (self, table, sort_by=[], **criteria):
        return table.select(*make_simple_select_arg(criteria,table),
                            **{'order_by':make_order_by(sort_by,table)}
                            ).execute().fetchall()

    def fetch_one (self, table, **criteria):
        """Fetch one item from table and arguments"""
        return table.select(*make_simple_select_arg(criteria,table)).execute().fetchone()

    def fetch_count (self, table, column, sort_by=[],**criteria):
        """Return a counted view of the table, with the count stored in the property 'count'"""
        result =  sqlalchemy.select(
            [sqlalchemy.func.count(getattr(table.c,column)),
             getattr(table.c,column)],
            group_by=column,
            order_by=make_order_by(sort_by,table,count_by=column)
            ).execute().fetchall()
        for row in result: row.count = row[0]
        return result

    def fetch_len (self, table, **criteria):
        """Return the number of rows in table that match criteria
        """
        return table.count().execute().fetchone()[0]

    def fetch_join (self, table1, table2, col1, col2,
                    column_names=None, sort_by=[], **criteria):
        if column_names:
            raise 'column_names KWARG NO LONGER SUPPORTED BY fetch_join!'
        print 'select=',make_simple_select_arg(criteria,table1,table2)
        return  table1.join(table2,getattr(table1.c,col1)==getattr(table2.c,col2)).select(
            *make_simple_select_arg(criteria,table1,table2)
            ).execute().fetchall()

    def fetch_food_groups_for_search (self, words):
        """Return food groups that match a given set of words."""
        where_statement = or_(
            *[self.nutrition_table.c.desc.like('%%%s%%'%w.lower())
              for w in words]
            )
        return [r[0] for r in sqlalchemy.select(
            [self.nutrition_table.c.foodgroup],
            where_statement,
            distinct=True).execute().fetchall()]

    def search_nutrition (self, words, group=None):
        """Search nutritional information for ingredient keys."""
        where_statement = and_(
            *[self.nutrition_table.c.desc.like('%%%s%%'%w)
              for w in words])
        if group:
            where_statement = and_(self.nutrition_table.c.foodgroup==group,
                                   where_statement)
        return self.nutrition_table.select(where_statement).execute().fetchall()

    def __get_joins (self, searches):
        joins = []
        for s in searches:
            if type(s)==tuple:
                joins.append(self.__get_joins(s[0]))
            else:
                if s['column'] == 'category':
                    if self.categories_table not in joins:
                        joins.append(self.categories_table,self.categories_table.c.id,
                                     self.recipe_table.c.id)
                elif s['column'] == 'ingredient':
                    if self.ingredients_table not in joins:
                        joins.append(self.ingredients_table)
        return joins

    def get_criteria (self,crit):
        if type(crit)==tuple:
            criteria,logic = crit
            if logic=='and':
                return and_(*[self.get_criteria(c) for c in criteria])
            elif logic=='or':
                return or_(*[self.get_criteria(c) for c in criteria])
        elif type(crit)!=dict: raise TypeError
        else:
            #join_crit = None # if we need to add an extra arg for a join
            if crit['column']=='category':
                subtable = self.categories_table
                col = subtable.c.category
            elif crit['column'] in ['ingkey','item']:
                subtable = self.ingredients_table
                col = getattr(subtable.c,crit['column'])
            elif crit['column']=='ingredient':
                d1 = crit.copy(); d1.update({'column':'ingkey'})
                d2 = crit.copy(); d2.update({'column':'item'}),
                return self.get_criteria(([d1,d2],
                                          'or'))
            elif crit['column']=='anywhere':
                searches = []
                for column in ['ingkey','item','category','cuisine','title','instructions','modifications',
                               'source','link']:
                    d = crit.copy(); d.update({'column':column})
                    searches.append(d)
                return self.get_criteria((searches,'or'))
            else:
                subtable = None
                col = getattr(self.recipe_table.c,crit['column'])
            if crit.get('operator','LIKE')=='LIKE':
                retval = (col.like(crit['search']))
            elif crit['operator']=='REGEXP':
                retval = (col.op('REGEXP')(crit['search']))
            else:
                retval = (col==crit['search'])
            if subtable:
                retval = self.recipe_table.c.id.op('in')(
                    sqlalchemy.select([subtable.c.recipe_id],retval)
                    )
            return retval

    def search_recipes (self, searches, sort_by=[]):
        """Search recipes for columns of values.

        "category" and "ingredient" are handled magically

        sort_by is a list of tuples (column,1) [ASCENDING] or (column,-1) [DESCENDING]
        """
        criteria = self.get_criteria((searches,'and'))
        return sqlalchemy.select([self.recipe_table],criteria,distinct=True,
                                 order_by=make_order_by(sort_by,self.recipe_table)
                                 ).execute().fetchall()

    def filter (self, table, func):
        """Return a table representing filtered with func.

        func is called with each row of the table.
        """
        raise NotImplementedError

    def get_unique_values (self, colname,table=None,**criteria):
        """Get list of unique values for column in table."""
        if not table: table=self.recipe_table
        if criteria: table = table.select(*make_simple_select_arg(criteria,table))
        if colname=='category' and table==self.recipe_table:
            print 'WARNING: you are using a hack to access category values.'
            table=self.categories_table
        retval = [r[0] for
                  r in sqlalchemy.select([getattr(table.c,colname)],distinct=True).execute().fetchall()
                  ]
        return filter(lambda x: x is not None, retval) # Don't return null values

    def get_ingkeys_with_count (self, search={}):
        """Get unique list of ingredient keys and counts for number of times they appear in the database.
        """
        raise NotImplementedError

    def delete_by_criteria (self, table, criteria):
        """Table is our table.
        Criteria is a dictionary of criteria to delete by.
        """
        criteria = fix_colnames(criteria,table)
        delete_args = []
        for k,v in criteria.items():
            delete_args.append(k==v)
        table.delete(*delete_args).execute()

    def update_by_criteria (self, table, update_criteria, new_values_dic):
        table.update(*make_simple_select_arg(update_criteria,table)).execute(**new_values_dic)

    def add_column_to_table (self, table, column_spec):
        """table is a table, column_spec is a tuple defining the
        column, following the format for new tables.
        """
        #column = Column(*column_spec)
        raise NotImplementedError

    def alter_table (self, table_name, setup_function, cols_to_change={}, cols_to_keep=[]):
        """Change table, moving some columns.

        table is the table object. table_name is the table
        name. setup_function is a function that will setup our correct
        table. cols_to_change is a list of columns that are changing
        names (key=orig, val=new). cols_to_keep is a list of columns
        that should be copied over as is.

        This works by renaming our table to a temporary name, then
        recreating our initial table. Finally, we copy over table
        data and then delete our temporary table (i.e. our old table)

        This is much less efficient than an alter table command, but
        will allow us to e.g. change/add primary key columns to sqlite
        tables
        """
        try:
            self.db.execute('ALTER TABLE %(t)s RENAME TO %(t)s_temp'%{'t':table_name})
        except:
            do_raise = True
            import traceback; traceback.print_exc()
            try:
                self.db.execute('DROP TABLE %(t)s_temp'%{'t':table_name})
            except:
                1
            else:
                do_raise = False
                self.db.execute('ALTER TABLE %(t)s RENAME TO %(t)s_temp'%{'t':table_name})
            if do_raise:
                raise 
        del self.metadata.tables[table_name]
        setup_function()
        getattr(self,'%s_table'%table_name).create()
        for row in self.db.execute('''SELECT %(cols)s FROM %(t)s_temp'''%{
            't':table_name,
            'cols':', '.join(cols_to_change.keys()+cols_to_keep)
            }).fetchall():
            newdic = {}
            for k in cols_to_change:
                newdic[cols_to_change[k]]=getattr(row,k)
            for c in cols_to_keep:
                newdic[c] = getattr(row,c)
            self.do_add(getattr(self,'%s_table'%table_name),newdic)
        self.db.execute('DROP TABLE %s_temp'%table_name)

    # Metakit has no AUTOINCREMENT, so it has to do special magic here
    def increment_field (self, table, field):
        """Increment field in table, or return None if the DB will do
        this automatically.
        """
        return None


    def row_equal (self, r1, r2):
        """Test whether two row references are the same.

        Return True if r1 and r2 reference the same row in the database.
        """
        return r1==r2

    def find_duplicates (self, by='recipe',recipes=None, include_deleted=True):
        """Find all duplicate recipes by recipe or ingredient.

        This uses the recipe_hash and ingredient_hash respectively.
        To find only those recipes that have both duplicate recipe and
        ingredient hashes, use find_all_duplicates
        """
        raise NotImplementedError

    def find_complete_duplicates (self, recipes=None, include_deleted=True):
        """Find all duplicate recipes (by recipe_hash and ingredient_hash)."""
        raise NotImplementedError
    
    # convenience DB access functions for working with ingredients,
    # recipes, etc.

    def delete_ing (self, ing):
        """Delete ingredient permanently."""
        self.delete_by_criteria(self.ingredients_table,
                                {'id':ing.id})

    def modify_rec (self, rec, dic):
        """Modify recipe based on attributes/values in dictionary.

        Return modified recipe.
        """
        self.validate_recdic(dic)        
        debug('validating dictionary',3)
        if dic.has_key('category'):
            newcats = dic['category'].split(', ')
            curcats = self.get_cats(rec)
            for c in curcats:
                if c not in newcats:
                    self.delete_by_criteria(self.categories_table,{'id':rec.id,'category':c})
            for c in newcats:
                if c not in curcats:
                    self.do_add_cat({'id':rec.id,'category':c})
            del dic['category']
        debug('do modify rec',3)
        return self.do_modify_rec(rec,dic)
    
    def validate_recdic (self, recdic):
        if not recdic.has_key('last_modified'):
            recdic['last_modified']=time.time()
        if recdic.has_key('image') and not recdic.has_key('thumb'):
            # if we have an image but no thumbnail, we want to create the thumbnail.
            try:
                img = ImageExtras.get_image_from_string(recdic['image'])
                thumb = ImageExtras.resize_image(img,40,40)
                ofi = StringIO.StringIO()
                thumb.save(ofi,'JPEG')
                recdic['thumb']=ofi.getvalue()
                ofi.close()
            except:
                del recdic['image']
                print """Warning: gourmet couldn't recognize the image.

                Proceding anyway, but here's the traceback should you
                wish to investigate.
                """
                import traceback
                traceback.print_stack()
        for k,v in recdic.items():
            try:
                recdic[k]=v.strip()
            except:
                pass

    def modify_ings (self, ings, ingdict):
        # allow for the possibility of doing a smarter job changing
        # something for a whole bunch of ingredients...
        for i in ings: self.modify_ing(i,ingdict)

    def modify_ing_and_update_keydic (self, ing, ingdict):
        """Update our key dictionary and modify our dictionary.

        This is a separate method from modify_ing because we only do
        this for hand-entered data, not for mass imports.
        """
        # If our ingredient has changed, update our keydic...
        if ing.item!=ingdict.get('item',ing.item) or ing.ingkey!=ingdict.get('ingkey',ing.ingkey):
            if ing.item and ing.ingkey:
                self.remove_ing_from_keydic(ing.item,ing.ingkey)
                self.add_ing_to_keydic(
                    ingdict.get('item',ing.item),
                    ingdict.get('ingkey',ing.ingkey)
                    )
        return self.modify_ing(ing,ingdict)
        
    def update_hashes (self, rec):
        rhash,ihash = recipeIdentifier.hash_recipe(rec,self)
        self.do_modify_rec(rec,{'recipe_hash':rhash,'ingredient_hash':ihash})

    def find_duplicates (self, rec, match_ingredient=True, match_recipe=True):
        """Return recipes that appear to be duplicates"""
        if match_ingredient and match_recipe:
            perfect_matches = self.fetch_all(ingredient_hash=rec.ingredient_hash,recipe_hash=rec.recipe_hash)
        elif match_ingredient:
            perfect_matches = self.fetch_all(ingredient_hash=rec.ingredient_hash)
        else:
            perfect_matches = self.fetch_all(recipe_hash=rec.recipe_hash)
        matches = []
        if len(perfect_matches) == 1:
            return []
        else:
            for r in perfect_matches:
                if r.id != rec.id:
                    matches.append(r)
            return matches

    def find_all_duplicates (self):
        """Return a list of sets of duplicate recipes."""
        raise NotImplementedError

    def merge_mergeable_duplicates (self):
        """Merge all duplicates for which a simple merge is possible.
        For those recipes which can't be merged, return:
        [recipe-id-list,to-merge-dic,diff-dic]
        """
        dups = self.find_all_duplicates()
        unmerged = []
        for recs in dups:
            rec_objs = [self.fetch_one(self.recipe_table,id=r) for r in recs]
            merge_dic,diffs = recipeIdentifier.merge_recipes(self,rec_objs)
            if not diffs:
                if merge_dic:
                    self.modify_rec(rec_objs[0],merge_dic)
                for r in rec_objs[1:]: self.delete_rec(r)
            else:
                unmerged.append([recs,merge_dic,diffs])
        return unmerged
    
    def modify_ing (self, ing, ingdict):
        self.validate_ingdic(ingdict)
        return self.do_modify_ing(ing,ingdict)

    def add_rec (self, dic):
        cats = []
        if dic.has_key('category'):
            cats = dic['category'].split(', ')
            del dic['category']
        if dic.has_key('servings'):
            dic['servings'] = float(dic['servings'])
        if not dic.has_key('deleted'): dic['deleted']=False
        self.validate_recdic(dic)
        try:
            ret = self.do_add_rec(dic)
        except:
            print 'Problem adding ',dic
            raise
        else:
            if type(ret)==int:
                ID = ret
            else:
                ID = ret.id
            for c in cats:
                if c: self.do_add_cat({'recipe_id':ID,'category':c})
            return ret

    def add_ing_and_update_keydic (self, dic):
        if dic.has_key('item') and dic.has_key('ingkey') and dic['item'] and dic['ingkey']:
            self.add_ing_to_keydic(dic['item'],dic['ingkey'])
        return self.add_ing(dic)
    
    def add_ing (self, dic):
        self.validate_ingdic(dic)
        try:          
            return self.do_add_ing(dic)
        except:
            print 'Problem adding',dic
            raise

    # Lower level DB access functions -- hopefully subclasses can
    # stick to implementing these    

    def do_add (self, table, dic):
        insert_statement = table.insert()
        result_proxy = insert_statement.execute(**dic)
        return result_proxy

    def do_add_and_return_item (self, table, dic, id_prop='id'):
        result_proxy = self.do_add(table,dic)
        select = table.select(getattr(table.c,id_prop)==result_proxy.lastrowid)
        return select.execute().fetchone()

    def do_add_ing (self,dic):
        return self.do_add_and_return_item(self.ingredients_table,dic,id_prop='id')

    def do_add_cat (self, dic):
        return self.do_add_and_return_item(self.categories_table,dic)

    def do_add_rec (self, rdict):
        """Add a recipe based on a dictionary of properties and values."""
        self.changed=True
        if not rdict.has_key('deleted'):
            rdict['deleted']=0
        insert_statement = self.recipe_table.insert()
        select = self.recipe_table.select(self.recipe_table.c.id==insert_statement.execute(**rdict).lastrowid)
        return select.execute().fetchone()

    def validate_ingdic (self,dic):
        """Do any necessary validation and modification of ingredient dictionaries."""
        if not dic.has_key('deleted'): dic['deleted']=False

    def do_modify_rec (self, rec, dic):
        """This is what other DBs should subclass."""
        return self.do_modify(self.recipe_table,rec,dic)

    def do_modify_ing (self, ing, ingdict):
        """modify ing based on dictionary of properties and new values."""
        return self.do_modify(self.ingredients_table,ing,ingdict)

    def do_modify (self, table, row, d, id_col='id'):
        if id_col:
            qr = table.update(getattr(table.c,id_col)==getattr(row,id_col)).execute(**d)
            select = table.select(getattr(table.c,id_col)==getattr(row,id_col))
        else:
            qr = table.update().execute(**d)
            select = table.select()
        return select.execute().fetchone()

    def get_ings (self, rec):
        """Handed rec, return a list of ingredients.

        rec should be an ID or an object with an attribute ID)"""
        if hasattr(rec,'id'):
            id=rec.id
        else:
            id=rec
        return self.fetch_all(self.ingredients_table,recipe_id=id,deleted=False)

    def get_cats (self, rec):
        svw = self.fetch_all(self.categories_table,recipe_id=rec.id)
        cats =  [c.category or '' for c in svw]
        # hackery...
        while '' in cats:
            cats.remove('')
        return cats

    def get_referenced_rec (self, ing):
        """Get recipe referenced by ingredient object."""
        if hasattr(ing,'refid') and ing.refid:
            rec = self.get_rec(ing.refid)
            if rec: return rec
        # otherwise, our reference is no use! Something's been
        # foobared. Unfortunately, this does happen, so rather than
        # screwing our user, let's try to look based on title/item
        # name (the name of the ingredient *should* be the title of
        # the recipe, though the user could change this)
        if hasattr(ing,'item'):
            rec = self.fetch_one(self.recipe_table,**{'title':ing.item})
            if rec:
                self.modify_ing(ing,{'refid':rec.id})
                return rec
            else:
                print 'Very odd: no match for',ing,'refid:',ing.refid

    def get_rec (self, id, recipe_table=None):
        """Handed an ID, return a recipe object."""
        if recipe_table:
            print 'handing get_rec an recipe_table is deprecated'
            print 'Ignoring recipe_table handed to get_rec'
        recipe_table=self.recipe_table
        return self.fetch_one(self.recipe_table, id=id)

    def delete_rec (self, rec):
        """Delete recipe object rec from our database."""
        if type(rec)!=int: rec=rec.id
        debug('deleting recipe ID %s'%rec,0)
        self.delete_by_criteria(self.recipe_table,{'id':rec})
        self.delete_by_criteria(self.categories_table,{'recipe_id':rec})
        self.delete_by_criteria(self.ingredients_table,{'recipe_id':rec})
        debug('deleted recipe ID %s'%rec,0)
        #raise NotImplementedError

    def new_rec (self):
        """Create and return a new, empty recipe"""
        blankdict = {'title':_('New Recipe'),
                     #'servings':'4'}
                     }
        return self.add_rec(blankdict)

    def new_id (self):
        raise NotImplementedError("WARNING: NEW_ID IS NO LONGER FUNCTIONAL, FIND A NEW WAY AROUND THE PROBLEM")
    
    # Convenience functions for dealing with ingredients

    def order_ings (self, ings):
        """Handed a view of ingredients, we return an alist:
        [['group'|None ['ingredient1', 'ingredient2', ...]], ... ]
        """
        defaultn = 0
        groups = {}
        group_order = {}
        n = 0; group = 0
        for i in ings:
            # defaults
            if not hasattr(i,'inggroup'):
                group = None
            else:
                group=i.inggroup
            if group == None:
                group = n; n+=1
            if not hasattr(i,'position'):
                print 'Bad: ingredient without position',i
                i.position=defaultn
                defaultn += 1
            if groups.has_key(group): 
                groups[group].append(i)
                # the position of the group is the smallest position of its members
                # in other words, positions pay no attention to groups really.
                if i.position < group_order[group]: group_order[group]=i.position
            else:
                groups[group]=[i]
                group_order[group]=i.position
        # now we just have to sort an i-listify
        def sort_groups (x,y):
            if group_order[x[0]] > group_order[y[0]]: return 1
            elif group_order[x[0]] == group_order[y[0]]: return 0
            else: return -1
        alist=groups.items()
        alist.sort(sort_groups)
        def sort_ings (x,y):
            if x.position > y.position: return 1
            elif x.position == y.position: return 0
            else: return -1
        for g,lst in alist:
            lst.sort(sort_ings)
        final_alist = []
        last_g = -1
        for g,ii in alist:
            if type(g)==int:
                if last_g == None:
                    final_alist[-1][1].extend(ii)
                else:
                    final_alist.append([None,ii])
                last_g = None
            else:
                final_alist.append([g,ii])
                last_g = g
        return final_alist

    def replace_ings (self, ingdicts):
        """Add a new ingredients and remove old ingredient list."""
        ## we assume (hope!) all ingdicts are for the same ID
        id=ingdicts[0]['id']
        debug("Deleting ingredients for recipe with ID %s"%id,1)
        self.delete_by_criteria(self.ingredients_table,{'id':id})
        for ingd in ingdicts:
            self.add_ing(ingd)
    
    def ingview_to_lst (self, view):
        """Handed a view of ingredient data, we output a useful list.
        The data we hand out consists of a list of tuples. Each tuple contains
        amt, unit, key, alternative?"""
        ret = []
        for i in view:
            ret.append([self.get_amount(i), i.unit, i.ingkey,])
        return ret

    def get_amount (self, ing, mult=1):
        """Given an ingredient object, return the amount for it.

        Amount may be a tuple if the amount is a range, a float if
        there is a single amount, or None"""
        amt=getattr(ing,'amount')
        try:
            ramt = getattr(ing,'rangeamount')
        except:
            # this blanket exception is here for our lovely upgrade
            # which requires a working export with a out-of-date DB
            ramt = None
        if mult != 1:
            if amt: amt = amt * mult
            if ramt: ramt = ramt * mult
        if ramt:
            return (amt,ramt)
        else:
            return amt

    def get_amount_and_unit (self, ing, mult=1, conv=None,fractions=convert.FRACTIONS_ALL):
        """Return a tuple of strings representing our amount and unit.
        
        If we are handed a converter interface, we will adjust the
        units to make them readable.
        """
        amt=self.get_amount(ing,mult)
        unit=ing.unit
        ramount = None
        if type(amt)==tuple: amt,ramount = amt
        if conv:
            amt,unit = conv.adjust_unit(amt,unit)
            if ramount and unit != ing.unit:
                # if we're changing units... convert the upper range too
                ramount = ramount * conv.converter(ing.unit, unit)
        if ramount: amt = (amt,ramount)
        return (self._format_amount_string_from_amount(amt,fractions=fractions),unit)
        
    def get_amount_as_string (self,
                              ing,
                              mult=1,
                              fractions=convert.FRACTIONS_ALL
                              ):
        """Return a string representing our amount.
        If we have a multiplier, multiply the amount before returning it.        
        """
        amt = self.get_amount(ing,mult)
        return self._format_amount_string_from_amount(amt, fractions=fractions)

    def _format_amount_string_from_amount (self, amt, fractions=convert.FRACTIONS_ALL):
        """Format our amount string given an amount tuple.

        If you're thinking of using this function from outside, you
        should probably just use a convenience function like
        get_amount_as_string or get_amount_and_unit
        """
        if type(amt)==tuple:
            return "%s-%s"%(convert.float_to_frac(amt[0],fractions=fractions).strip(),
                            convert.float_to_frac(amt[1],fractions=fractions).strip())
        elif type(amt)==float:
            return convert.float_to_frac(amt,fractions=fractions)
        else: return ""

    def get_amount_as_float (self, ing, mode=1): #1 == self.AMT_MODE_AVERAGE
        """Return a float representing our amount.

        If we have a range for amount, this function will ignore the range and simply
        return a number.  'mode' specifies how we deal with the mode:
        self.AMT_MODE_AVERAGE means we average the mode (our default behavior)
        self.AMT_MODE_LOW means we use the low number.
        self.AMT_MODE_HIGH means we take the high number.
        """
        amt = self.get_amount(ing)
        if type(amt) in [float, type(None)]:
            return amt
        else:
            # otherwise we do our magic
            amt=list(amt)
            amt.sort() # make sure these are in order
            low,high=amt
            if mode==self.AMT_MODE_AVERAGE: return (low+high)/2.0
            elif mode==self.AMT_MODE_LOW: return low
            elif mode==self.AMT_MODE_HIGH: return high # mode==self.AMT_MODE_HIGH
            else:
                raise ValueError("%s is an invalid value for mode"%mode)
    
    def add_ing_to_keydic (self, item, key):
        #print 'add ',item,key,'to keydic'
        if not item or not key: return
        row = self.fetch_one(self.keylookup_table, item=item, ingkey=key)
        if row:
            self.do_modify(self.keylookup_table,row,{'count':row.count+1})
        else:
            self.do_add(self.keylookup_table,{'item':item,'ingkey':key,'count':1})
        for w in item.split():
            w=str(w.decode('utf8').lower())
            row = self.fetch_one(self.keylookup_table,word=w,ingkey=key)
            if row:
                self.do_modify(self.keylookup_table,row,{'count':row.count+1})
            else:
                self.do_add(self.keylookup_table,{'word':w,'ingkey':key,'count':1})

    def remove_ing_from_keydic (self, item, key):
        #print 'remove ',item,key,'to keydic'        
        row = self.fetch_one(self.keylookup_table,item=item,ingkey=key)
        if row:
            new_count = row.count - 1
            if new_count:
                self.do_modify(self.keylookup_table,row,{'count':new_count})
            else:
                self.delete_by_criteria(self.keylookup_table,{'item':item,'ingkey':key})
        for w in item.split():
            w=str(w.decode('utf8').lower())
            row = self.fetch_one(self.keylookup_table,item=item,ingkey=key)
            if row:
                new_count = row.count - 1
                if new_count:
                    self.do_modify(self.keylookup_table,row,{'count':new_count})
                else:
                    self.delete_by_criteria(self.keylookup_table,{'word':w,'ingkey':key})

    def ing_shopper (self, view):
        return DatabaseShopper(self.ingview_to_lst(view))

    # functions to undoably modify tables 

    def get_dict_for_obj (self, obj, keys):
        orig_dic = {}
        for k in keys:
            if k=='category':
                v = ", ".join(self.get_cats(obj))
            else:
                v=getattr(obj,k)
            orig_dic[k]=v
        return orig_dic

    def undoable_modify_rec (self, rec, dic, history=[], get_current_rec_method=None,
                             select_change_method=None):
        """Modify our recipe and remember how to undo our modification using history."""
        orig_dic = self.get_dict_for_obj(rec,dic.keys())
        reundo_name = "Re_apply"
        reapply_name = "Re_apply "
        reundo_name += string.join(["%s <i>%s</i>"%(k,v) for k,v in orig_dic.items()])
        reapply_name += string.join(["%s <i>%s</i>"%(k,v) for k,v in dic.items()])
        redo,reundo=None,None
        if get_current_rec_method:
            def redo (*args):
                r=get_current_rec_method()
                odic = self.get_dict_for_obj(r,dic.keys())
                return ([r,dic],[r,odic])
            def reundo (*args):
                r = get_current_rec_method()
                odic = self.get_dict_for_obj(r,orig_dic.keys())
                return ([r,orig_dic],[r,odic])

        def action (*args,**kwargs):
            """Our actual action allows for selecting changes after modifying"""
            self.modify_rec(*args,**kwargs)
            if select_change_method:
                select_change_method(*args,**kwargs)
                
        obj = Undo.UndoableObject(action,action,history,
                                  action_args=[rec,dic],undo_action_args=[rec,orig_dic],
                                  get_reapply_action_args=redo,
                                  get_reundo_action_args=reundo,
                                  reapply_name=reapply_name,
                                  reundo_name=reundo_name,)
        obj.perform()

    def undoable_delete_recs (self, recs, history, make_visible=None):
        """Delete recipes by setting their 'deleted' flag to True and add to UNDO history."""
        def do_delete ():
            for rec in recs:
                debug('rec %s deleted=True'%rec.id,1)
                self.modify_rec(rec,{'deleted':True})
            if make_visible: make_visible(recs)
        def undo_delete ():
            for rec in recs:
                debug('rec %s deleted=False'%rec.id,1)
                self.modify_rec(rec,{'deleted':False})
            if make_visible: make_visible(recs)
        obj = Undo.UndoableObject(do_delete,undo_delete,history)
        obj.perform()

    def undoable_modify_ing (self, ing, dic, history, make_visible=None):
        """modify ingredient object ing based on a dictionary of properties and new values.

        history is our undo history to be handed to Undo.UndoableObject
        make_visible is a function that will make our change (or the undo or our change) visible.
        """
        orig_dic = self.get_dict_for_obj(ing,dic.keys())
        key = dic.get('ingkey',None)
        item = key and dic.get('item',ing.item)
        def do_action ():
            debug('undoable_modify_ing modifying %s'%dic,2)
            self.modify_ing(ing,dic)
            if key:
                self.add_ing_to_keydic(item,key)
            if make_visible: make_visible(ing,dic)
        def undo_action ():
            debug('undoable_modify_ing unmodifying %s'%orig_dic,2)
            self.modify_ing(ing,orig_dic)
            if key:
                self.remove_ing_from_keydic(item,key)
            if make_visible: make_visible(ing,orig_dic)
        obj = Undo.UndoableObject(do_action,undo_action,history)
        obj.perform()
        
    def undoable_delete_ings (self, ings, history, make_visible=None):
        """Delete ingredients in list ings and add to our undo history."""
        def do_delete():
            modded_ings = [self.modify_ing(i,{'deleted':True}) for i in ings]
            if make_visible:
                make_visible(modded_ings)
        def undo_delete ():
            modded_ings = [self.modify_ing(i,{'deleted':False}) for i in ings]
            if make_visible: make_visible(modded_ings)
        obj = Undo.UndoableObject(do_delete,undo_delete,history)
        obj.perform()
    
    def get_default_values (self, colname):
        try:
            return defaults.fields[colname]
        except:
            return []

    
class RecipeManager (RecData):
    
    def __init__ (self,*args,**kwargs):
        debug('recipeManager.__init__()',3)
        RecData.__init__(self,*args,**kwargs)
        self.km = keymanager.KeyManager(rm=self)
        
    def key_search (self, ing):
        """Handed a string, we search for keys that could match
        the ingredient."""
        result=self.km.look_for_key(ing)
        if type(result)==type(""):
            return [result]
        elif type(result)==type([]):
            # look_for contains an alist of sorts... we just want the first
            # item of every cell.
            if len(result)>0 and result[0][1]>0.8:
                return map(lambda a: a[0],result)
            else:
                ## otherwise, we make a mad attempt to guess!
                k=self.km.generate_key(ing)
                l = [k]
                l.extend(map(lambda a: a[0],result))
                return l
        else:
            return None
            
    def ingredient_parser (self, s, conv=None, get_key=True):
        """Handed a string, we hand back a dictionary representing a parsed ingredient (sans recipe ID)"""
        debug('ingredient_parser handed: %s'%s,0)
        s = unicode(s) # convert to unicode so our ING MATCHER works properly
        s=s.strip("\n\t #*+-")
        debug('ingredient_parser handed: "%s"'%s,1)
        m=convert.ING_MATCHER.match(s)
        if m:
            debug('ingredient parser successfully parsed %s'%s,1)
            d={}
            a,u,i=(m.group(convert.ING_MATCHER_AMT_GROUP),
                   m.group(convert.ING_MATCHER_UNIT_GROUP),
                   m.group(convert.ING_MATCHER_ITEM_GROUP))
            if a:
                asplit = convert.RANGE_MATCHER.split(a)
                if len(asplit)==2:
                    d['amount']=convert.frac_to_float(asplit[0].strip())
                    d['rangeamount']=convert.frac_to_float(asplit[1].strip())
                else:
                    d['amount']=convert.frac_to_float(a.strip())
            if u:
                if conv and conv.unit_dict.has_key(u.strip()):
                    # Don't convert units to our units!
                    d['unit']=u.strip()
                else:
                    # has this unit been used
                    prev_uses = self.fetch_all(self.ingredients_table,unit=u.strip())
                    if prev_uses:
                        d['unit']=u
                    else:
                        # otherwise, unit is not a unit
                        i = u + ' ' + i
            if i:
                optmatch = re.search('\s+\(?[Oo]ptional\)?',i)
                if optmatch:
                    d['optional']=True
                    i = i[0:optmatch.start()] + i[optmatch.end():]
                d['item']=i.strip()
                if get_key: d['ingkey']=self.km.get_key(i.strip())
            debug('ingredient_parser returning: %s'%d,0)
            return d
        else:
            debug("Unable to parse %s"%s,0)
            return None

    def ing_search (self, ing, keyed=None, recipe_table=None, use_regexp=True, exact=False):
        """Search for an ingredient."""
        if not recipe_table: recipe_table = self.recipe_table
        vw = self.joined_search(recipe_table,self.ingredients_table,'ingkey',ing,use_regexp=use_regexp,exact=exact)
        if not keyed:
            vw2 = self.joined_search(recipe_table,self.ingredients_table,'item',ing,use_regexp=use_regexp,exact=exact)
            if vw2 and vw:
                vw = vw.union(vw2)
            else: vw = vw2
        return vw

    def joined_search (self, table1, table2, search_by, search_str, use_regexp=True, exact=False, join_on='id'):
        raise NotImplementedError
    
    def ings_search (self, ings, keyed=None, recipe_table=None, use_regexp=True, exact=False):
        """Search for multiple ingredients."""
        raise NotImplementedError

    def clear_remembered_optional_ings (self, recipe=None):
        """Clear our memories of optional ingredient defaults.

        If handed a recipe, we clear only for the recipe we've been
        given.

        Otherwise, we clear *all* recipes.
        """
        if recipe:
            vw = self.get_ings(recipe)
        else:
            vw = self.ingredients_table
        # this is ugly...
        vw1 = vw.select(shopoptional=1)
        vw2 = vw.select(shopoptional=2)
        for v in vw1,vw2:
            for i in v: self.modify_ing(i,{'shopoptional':0})

class DatabaseConverter(convert.Converter):
    def __init__ (self, db):
        self.db = db
        convert.converter.__init__(self)
    ## FIXME: still need to finish this class and then
    ## replace calls to convert.converter with
    ## calls to DatabaseConverter

    def create_conv_table (self):
        self.conv_table = dbDic('ckey','value',self.db.convtable_table, self.db,
                                pickle_key=True)
        for k,v in defaults.CONVERTER_TABLE.items():
            if not self.conv_table.has_key(k):
                self.conv_table[k]=v

    def create_density_table (self):
        self.density_table = dbDic('dkey','value',
                                   self.db.density_table,self.db)
        for k,v in defaults.DENSITY_TABLE.items():
            if not self.density_table.has_key(k):
                self.density_table[k]=v

    def create_cross_unit_table (self):
        self.cross_unit_table=dbDic('cukey','value',self.db.crossunitdict_table,self.db)
        for k,v in defaults.CROSS_UNIT_TABLE:
            if not self.cross_unit_table.has_key(k):
                self.cross_unit_table[k]=v

    def create_unit_dict (self):
        self.units = defaults.UNITS
        self.unit_dict=dbDic('ukey','value',self.db.unitdict_table,self.db)
        for itm in self.units:
            key = itm[0]
            variations = itm[1]
            self.unit_dict[key] = key
            for v in variations:
                self.unit_dict[v] = key
                
class dbDic:
    def __init__ (self, keyprop, valprop, view, db, pickle_key=False, pickle_val=True):
        """Create a dictionary interface to a database table."""
        self.pickle_key = pickle_key
        self.pickle_val = pickle_val
        self.vw = view
        self.kp = keyprop
        self.vp = valprop
        self.db = db
        self.just_got = {}

    def has_key (self, k):
        try:
            self.just_got = {k:self.__getitem__(k)}
            return True
        except:
            try:
                self.__getitem__(k)
                return True
            except:
                return False
        
    def __setitem__ (self, k, v):
        if self.pickle_key:
            k=pickle.dumps(k)
        if self.pickle_val: store_v=pickle.dumps(v)
        else: store_v = v
        row = self.db.fetch_one(self.vw,**{self.kp:k})
        if row:
            self.db.do_modify(self.vw, row, {self.vp:store_v})
        else:
            self.db.do_add(self.vw,{self.kp:k,self.vp:store_v})
        self.db.changed=True
        return v

    def __getitem__ (self, k):
        if self.just_got.has_key(k): return self.just_got[k]
        if self.pickle_key:
            k=pickle.dumps(k)
        v = getattr(self.db.fetch_one(self.vw,**{self.kp:k}),self.vp)
        if v and self.pickle_val:
            try:
                return pickle.loads(v)
            except:
                print "Problem unpickling ",v
                raise
        else:
            return v
    
    def __repr__ (self):
        retstr = "<dbDic> {"
        #for i in self.vw:
        #    if self.pickle_key:
        #        retstr += "%s"%pickle.loads(getattr(i,self.kp))
        #    else:
        #        retstr += getattr(i,self.kp)
        #    retstr += ":"
        #    if self.pickle_val:
        #        retstr += "%s"%pickle.loads(getattr(i,self.vp))
        #    else:
        #        retstr += "%s"%getattr(i,self.vp)
        #    retstr += ", "
        retstr += "}"
        return retstr

    def keys (self):
        ret = []
        for i in self.db.fetch_all(self.vw):
            ret.append(getattr(i,self.kp))
        return ret

    def values (self):
        ret = []
        for i in self.db.fetch_all(self.vw):
            val = getattr(i,self.vp)
            if val and self.pickle_val: val = pickle.loads(val)
            ret.append(val)
        return ret

    def items (self):
        ret = []
        for i in self.db.fetch_all(self.vw):
            key = getattr(i,self.kp)
            val = getattr(i,self.vp)
            if key and self.pickle_key:
                try:
                    key = pickle.loads(key)
                except:
                    print 'Problem unpickling key ',key
                    raise
            if val and self.pickle_val:
                try:
                    val = pickle.loads(val)
                except:
                    print 'Problem unpickling value ',val, ' for key ',key
                    raise 
            ret.append((key,val))
        return ret

# To change
# fetch_one -> use whatever syntax sqlalchemy uses throughout
# fetch_all ->
#recipe_table -> recipe_table
# To eliminate

def test_db ():
    import tempfile
    db = RecData(file=tempfile.mktemp())
    print 'BEGIN TESTING'
    from db_tests import test_db
    test_db(db)
    print 'END TESTING'

def add_sample_recs ():
    for rec,ings in [[dict(title='Spaghetti',cuisine='Italian',category='Easy, Entree'),
                      [dict(amount=1,unit='jar',item='Marinara Sauce',ingkey='sauce, marinara'),
                       dict(amount=0.25,unit='c.',item='Parmesan Cheese',ingkey='cheese, parmesan'),
                       dict(amount=.5,unit='lb.',item='Spaghetti',ingkey='spaghetti, dried')]],
                     [dict(title='Spaghetti w/ Meatballs',cuisine='Italian',category='Easy, Entree'),
                      [dict(amount=1,unit='jar',item='Marinara Sauce',ingkey='sauce, marinara'),
                       dict(amount=0.25,unit='c.',item='Parmesan Cheese',ingkey='cheese, parmesan'),
                       dict(amount=.5,unit='lb.',item='Spaghetti',ingkey='spaghetti, dried'),
                       dict(amount=0.5,unit='lb.',item='Meatballs',ingkey='Meatballs, prepared'),
                       ]],
                     [dict(title='Toasted cheese',cuisine='American',category='Sandwich, Easy',
                           servings=2),
                      [dict(amount=2,unit='slices',item='bread'),
                       dict(amount=2,unit='slices',item='cheddar cheese'),
                       dict(amount=2,unit='slices',item='tomato')]]
                     ]:
        r = db.add_rec(rec)
        for i in ings:
            i['recipe_id']=r.id
            db.add_ing(i)