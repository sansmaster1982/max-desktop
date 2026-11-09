"""Pinned WEB68/60 DTO contracts; pure synthetic data, no transport or accounts."""
from copy import deepcopy
import unittest

from maxclient.protocol.search_contracts import (
    SearchContractError, build_global_search, build_message_search,
    parse_global_search, parse_message_search,
)


def chat(identity=2**60 + 9):
    return {'id': identity, 'type': 'DIALOG', 'status': 'ACTIVE', 'owner': 7,
            'participants': {'7': 1000, '9': 1001}, 'lastEventTime': 1500, 'created': 500,
            'title': '<b>Literal title</b>'}


def message(identity=2**61 + 3):
    return {'id': identity, 'time': 1200.5, 'type': 'USER', 'text': '<img src=x> literal text'}


def contact(identity=2**60 + 13):
    return {'contact': {'id': identity, 'updateTime': 900,
                        'names': [{'firstName': 'Имя', 'lastName': 'Фамилия', 'type': 'ONEME'}]},
            'summary': 'Описание'}


class SearchContractsTests(unittest.TestCase):
    def test_exact_observed_initial_requests(self):
        self.assertEqual(build_message_search('test'), {'query':'test', 'count':30})
        self.assertEqual(build_global_search('test'), {'query':'test', 'count':40, 'type':'ALL'})
        self.assertEqual(build_message_search(' spaced '), {'query':' spaced ', 'count':30})

    def test_empty_query_and_invalid_count_cannot_form_an_rpc(self):
        for builder in (build_message_search, build_global_search):
            for query in ('', None, 123, False):
                with self.subTest(builder=builder.__name__,query=query),self.assertRaises(SearchContractError): builder(query)
            for count in (0,-1,True,1.5,1001):
                with self.subTest(count=count),self.assertRaises(SearchContractError): builder('test',count=count)

    def test_marker_families_are_distinct_and_zero_or_empty_is_retained(self):
        self.assertEqual(build_message_search('q',marker='')['marker'],'')
        self.assertEqual(build_global_search('q',marker=0)['marker'],0)
        self.assertEqual(build_global_search('q',marker=2**63-1)['marker'],2**63-1)
        for marker in (0,True,b'cursor'):
            with self.assertRaises(SearchContractError): build_message_search('q',marker=marker)
        for marker in ('0',True,2**63):
            with self.assertRaises(SearchContractError): build_global_search('q',marker=marker)

    def test_source_global_filter_enum_without_guessed_types(self):
        for kind in ('ALL','CHANNELS','PUBLIC_CHATS'):
            self.assertEqual(build_global_search('q',search_type=kind)['type'],kind)
        for kind in ('PEOPLE','MESSAGES',{},None):
            with self.assertRaises(SearchContractError): build_global_search('q',search_type=kind)

    def test_message_ack_preserves_i64_ids_time_plain_text_and_optional_sender(self):
        row={'chatId':chat()['id'],'section':'MESSAGES','message':message(),'chat':chat(),'highlights':['literal']}
        page=parse_message_search({'result':[row],'marker':'next','counters':{'MESSAGES':2}})
        self.assertEqual(page['total'],0)  # Zg's explicit current schema default.
        result=page['result'][0]
        self.assertEqual(result['message']['id'],2**61+3)
        self.assertEqual(result['chatId'],2**60+9)
        self.assertEqual(result['message']['time'],1200.5)
        self.assertEqual(result['message']['text'],'<img src=x> literal text')
        self.assertNotIn('sender',result['message'])
        self.assertNotIn('unavailable',result)

    def test_no_marker_or_section_or_counters_is_invented(self):
        page=parse_message_search({'result':[{'chatId':4,'highlights':[]} ]})
        self.assertEqual(page,{'result':[{'chatId':4,'highlights':[]}],'total':0})
        self.assertNotIn('marker',page)
        self.assertEqual(parse_message_search({'result':[],'marker':''})['marker'],'')

    def test_global_contact_remains_contact_without_fabricated_chat_id(self):
        page=parse_global_search({'result':[{'contact':contact(),'highlights':['им']}],'total':1,'marker':0})
        row=page['result'][0]
        self.assertEqual(row['contact']['contact']['id'],2**60+13)
        self.assertNotIn('chatId',row)
        self.assertNotIn('chat',row)
        self.assertEqual(page['marker'],0)

    def test_invalid_partial_nested_dtos_are_unavailable_not_repaired(self):
        rows=[{'chatId':7,'section':'MESSAGES','highlights':[], 'message':{'id':9,'text':'no time/type'}},
              {'chatId':True,'section':'CHATS','highlights':[], 'chat':{'id':True}},
              {'chatId':7,'section':'FUTURE','highlights':[]}]
        result=parse_message_search({'result':rows})['result']
        self.assertNotIn('message',result[0]);self.assertIn('message',result[0]['unavailable'])
        self.assertNotIn('chat',result[1]);self.assertNotIn('chatId',result[1])
        self.assertIn('section',result[2]['unavailable']);self.assertNotIn('section',result[2])

    def test_declared_message_section_requires_actual_message(self):
        row=parse_message_search({'result':[{'chatId':1,'section':'MESSAGES','highlights':[]} ]})['result'][0]
        self.assertEqual(row['unavailable'],('message',))

    def test_chat_row_identity_disagreement_is_not_silently_reassigned(self):
        row=parse_message_search({'result':[{'chatId':1,'chat':chat(2),'highlights':[]} ]})['result'][0]
        self.assertEqual(row['chatId'],1);self.assertNotIn('chat',row)
        self.assertIn('chat',row['unavailable'])

    def test_bad_contact_wrapper_and_message_without_chat_are_unavailable(self):
        result=parse_global_search({'result':[{'contact':{'id':7},'highlights':[]},
                                             {'message':message(),'highlights':[]}],'total':2})['result']
        self.assertIn('contact',result[0]['unavailable'])
        self.assertNotIn('contact',result[0])
        self.assertIn('identity',result[1]['unavailable'])
        self.assertNotIn('chatId',result[1])

    def test_optional_nested_field_wrong_type_never_reaches_core_parser(self):
        bad_message=message();bad_message['sender']=True
        bad_contact=contact();bad_contact['contact']['names'][0]['firstName']={'html':'untrusted'}
        row=parse_message_search({'result':[{'chatId':1,'highlights':[],'message':bad_message}]})['result'][0]
        self.assertNotIn('message',row)
        row=parse_global_search({'result':[{'highlights':[],'contact':bad_contact}],'total':1})['result'][0]
        self.assertNotIn('contact',row)

    def test_top_level_bad_total_marker_and_counter_fail_closed(self):
        cases=[{'result':[],'total':True},{'result':[],'marker':0},{'result':[],'marker':None},
               {'result':[],'counters':{'MESSAGES':False}}, {'result':'not-array'}]
        for data in cases:
            with self.subTest(data=data),self.assertRaises(SearchContractError):parse_message_search(data)
        for data in ({'result':[]},{'result':[],'total':1,'marker':'next'}):
            with self.assertRaises(SearchContractError):parse_global_search(data)

    def test_unknown_rich_payload_is_not_forwarded_and_input_is_not_mutated(self):
        raw={'result':[{'chatId':chat()['id'],'highlights':['x'],'chat':chat(),
                        'message':{**message(),'attaches':[{'_type':'FUTURE','rawSecret':'synthetic'}]}}]}
        snapshot=deepcopy(raw);projected=parse_message_search(raw)
        self.assertEqual(raw,snapshot)
        self.assertNotIn('attaches',projected['result'][0]['message'])
        projected['result'][0]['chat']['participants']['7']=0
        self.assertEqual(raw['result'][0]['chat']['participants']['7'],1000)

    def test_nonfinite_times_and_nonstring_highlights_have_no_display_dto(self):
        invalid=message();invalid['time']=float('nan')
        row=parse_message_search({'result':[{'chatId':1,'highlights':[{'html':'x'}],'message':invalid}]})['result'][0]
        self.assertNotIn('message',row);self.assertNotIn('highlights',row)


if __name__=='__main__':unittest.main()
