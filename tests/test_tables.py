from canav.analysis.tables import read_table


def test_read_table_plain_and_aligned(tmp_path):
    plain = tmp_path / "plain.csv"
    plain.write_text('k,name,value,note\n2,"a, b",0.5,\n10,c,1e-3,x\n')
    aligned = tmp_path / "aligned.csv"  # what a CSV viewer's "align columns" saves
    aligned.write_text(
        'k , name    , value , note\n2 , "a, b"  , 0.5   ,\n10, c       , 1e-3  , x   \n'
    )
    a, b = read_table(plain), read_table(aligned)
    assert list(a.columns) == list(b.columns) == ["k", "name", "value", "note"]
    assert a["k"].tolist() == b["k"].tolist() == [2, 10]
    assert a["name"].tolist() == b["name"].tolist() == ["a, b", "c"]
    assert a["value"].tolist() == b["value"].tolist() == [0.5, 0.001]
