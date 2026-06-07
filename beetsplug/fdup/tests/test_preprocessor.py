"""Tests for the TextPreprocessor class."""

import textwrap

import bitarray.util
import pandas as pd
import pytest

from beetsplug.fdup.fdup import TextPreprocessor, FDupPlugin


class TestTextPreprocessor:
    """Test suite for TextPreprocessor."""

    def test_blah(self):

        proc = TextPreprocessor(alphabet="01 ", word_size=60)
        print("\n")
        proc.text2bitarray("0.1")

    def test_blah_2(self):

        word_size = 1536
        # word_size = 384
        diff_pen = 0.025
        proc = TextPreprocessor(word_size=word_size)
        print("\n")

        # words = ["Poison", "Poison (live)", "Scouting for Girls", "Girlscouts"]
        # words = [
        #     "Whiskeytown",
        #     "Bing Crosby with John Scott Trotter and His Orchestra",
        #     "Richard Cooke, Royal Choral Society, BBC Concert Orchestra, Barry Wordsworth",
        #     "Robert Reinis, Zack Whedon, Maurissa Tancharoen, Stacy Shirk, Steve Berg, Felicia Day, Nathan Fillion, David Fury, Marti Noxon & Neil Patrick Harris"]
        # words = [
        #     "Excuse Me While I Break My Own Heart Tonight (acoustic)",
        #     "Jerusalem",
        #     "Too Romantic",
        #     "Moonlight Becomes You",
        #     "So They Say"
        # ]
        words = [
            "Don't Break My Heart Again",
            "You’re Gonna Break My Heart Again",
            "You’re Gonna Break My Heart Again - 2022 Remix",
            "Now You’re Gone",
            "Now You’re Gone - 2022 Remix"
        ]
        # words= [
        #     "Joker & the Thief",
        #     "Joker and the Thief"
        # ]
        # words = [
        #   "(Is This the Way to) The World Cup",
        #   "(Is This the Way to) Amarillo"
        # ]
        # words = [
        #   "Choir of King’s College, Cambridge, Philharmonia Orchestra, Philip Ledger",
        #   "Manchester Orchestra"
        # ]
        # words = [
        #     "Sunday Bloody Sunday (live from The Fleet Center, Boston, MA, USA / 2001)",
        #     "With or Without You (live from The Fleet Center, Boston, MA, USA / 2001)",
        #     "Stay (Faraway, So Close!) (live from The Fleet Center, Boston, MA, USA / 2001)"
        # ]
        # words = [
        #     "ABCDE",
        #     "ABCDE ABCDE",
        #     "ABCDE ABCDE ABCDE",
        # ]

        for word in words:
            result = proc.text2bitarray(word)
            print(f"{bitarray.util.ba2base(64, result)}: {word}")
        print("\n")

        plugin = FDupPlugin()

        table = []
        for i, word1 in enumerate(words):
            table.append([])
            for j, word2 in enumerate(words):
                result1 = proc.text2fingerprint(word1)
                result2 = proc.text2fingerprint(word2)
                n1 = result1.n
                n2 = result2.n
                n = bitarray.util.count_and(result1.bits, result2.bits)
                exp = (n1 * n2) / word_size
                pen = diff_pen*(n1 + n2 - 2 * n)
                eta = 1.0 / word_size
                denom = min(n1, n2)
                # val = n / denom
                val = (n - exp - pen + eta) / (denom - exp + eta)
                table[i].append(val)
                val2 = plugin._compare_fps(result1, result2)
                print(f"{word1} vs {word2}: ({n}-{exp:.1f}-{pen:.1f}+{eta:.1f})/({denom}-{exp:.1f}+{eta:.1f}) = {val:.3f}  ... cf. {val2:.3f}")
                # print(f"{word1} vs {word2}: {n}/{denom} = {val:.3f}")


        shortwords = [textwrap.shorten(word, width=20, placeholder="...") for word in words]
        df = pd.DataFrame(table, columns = shortwords, index=shortwords)
        pd.set_option('display.max_columns', None)
        pd.set_option('display.expand_frame_repr', False)
        print()
        print('\n'.join(words))
        print()
        print(df)

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
